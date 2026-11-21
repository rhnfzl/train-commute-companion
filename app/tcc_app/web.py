"""The HTTP side on a tiny asyncio server: `/health` for an uptime check (7.11, 7.12 rule 22) and
`POST /owntracks` for OwnTracks HTTP mode (8), and the read-only MCP read-out on a private Unix
socket (9, 7.12 rule 15: local only)."""

import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
import traceback
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any

from train_commute_companion.calendar_export import CalendarExportError, export_ics, export_id
from train_commute_companion.model import SentAlert
from train_commute_companion.route_export import export_preview
from train_commute_companion.view import RuntimeView

from .ntfy import NtfyProof
from .today import ICONS, MANIFEST, MANIFEST_PATH, PAUSED_PATH, TODAY_PATH
from .today import render as render_today

log = logging.getLogger(__name__)

HEAD_LIMIT = 16 * 1024
BODY_LIMIT = 64 * 1024
READ_TIMEOUT_S = 10.0
MCP_REQUEST_MAX = 4096
MCP_REPLY_MAX = 1_048_576
MCP_TIMEOUT_S = 5.0
MCP_ACTION_TIMEOUT_S = 20.0
MCP_ACTION_MAX = 32 * 1024
CALENDAR_SUBSCRIPTION = "/calendar/subscription/"
TODAY_HEADERS = (
    "Cache-Control: no-store\r\n"
    "Referrer-Policy: no-referrer\r\n"
    "X-Frame-Options: DENY\r\n"
    "X-Content-Type-Options: nosniff\r\n"
    "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
TODAY_PAGE_HEADERS = TODAY_HEADERS + "; manifest-src 'self'; img-src 'self'"


def health_report(
    view: RuntimeView, alive: bool, confirmed: bool, webhooks_failing: bool | None = None
) -> tuple[int, dict[str, Any]]:
    """Board freshness, look-ahead and webhook health are shown, not folded into `ok`: the loud
    `cannot_see_train` covers freshness, a failed planned-works lookup should not page, and the
    phone, not a webhook, is the delivery that counts. `webhooks_failing` is None without
    webhooks."""
    ok = (
        alive
        and view.started
        and view.store_ok
        and not view.key_rejected
        and view.refusal is None
        and view.undelivered == 0
        and confirmed
    )
    body = {
        "ok": ok,
        "alive": alive,
        "started": view.started,
        "store_ok": view.store_ok,
        "key_rejected": view.key_rejected,
        "profile_refused": view.refusal,
        "undelivered": view.undelivered,
        "delivery_confirmed": confirmed,
        "webhooks_failing": webhooks_failing,
        "requests": view.requests,
        "occurrences": [
            {
                "profile_id": o.key.profile_id,
                "direction": o.key.direction.value,
                "service_date": o.key.service_date.isoformat(),
                "state": o.direction_state.value,
                "fresh": o.health.fresh,
                "last_ok_at": o.health.last_ok_at.isoformat() if o.health.last_ok_at else None,
                "consecutive_failures": o.health.consecutive_failures,
                "auth_failed": o.health.auth_failed,
            }
            for o in view.occurrences
        ],
        "lookahead_last_ok": {
            k: v.isoformat() if v else None for k, v in view.lookahead_last_ok.items()
        },
        "attribution": "Data: NS",
    }
    return (200 if ok else 503), body


class _Refused(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


async def _read(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    """Method, path without the query string, headers (lower-case names), body. Raises
    `_Refused` with 400 or 413."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
        raise _Refused(400) from None
    lines = head[:-4].decode("latin-1").split("\r\n")
    words = lines[0].split(" ")
    if len(words) != 3 or not words[2].startswith("HTTP/"):
        raise _Refused(400)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            raise _Refused(400)
        headers[name.strip().lower()] = value.strip()
    length = headers.get("content-length", "0")
    # shortcut: no chunked bodies; OwnTracks sends a Content-Length
    if "transfer-encoding" in headers or not (length.isascii() and length.isdigit()):
        raise _Refused(400)
    # the length check first: int() raises ValueError on a very long run of digits
    if len(length) > len(str(BODY_LIMIT)) or int(length) > BODY_LIMIT:
        raise _Refused(413)
    try:
        body = await reader.readexactly(int(length))
    except asyncio.IncompleteReadError:
        raise _Refused(400) from None
    return words[0], words[1].split("?", 1)[0], headers, body


def _authorized(header: str | None, secret: str) -> bool:
    """HTTP Basic whose password is the secret; the user name is ignored."""
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(decoded.partition(b":")[2], secret.encode())


def _bearer_authorized(header: str | None, token: str) -> bool:
    scheme, _, given = (header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(given.encode(), token.encode())


def _today_authorized(header: str | None, token: str) -> bool:
    return _bearer_authorized(header, token) or _authorized(header, token)


def _response(
    status: int,
    body: bytes = b"",
    content_type: str = "application/json",
    extra: str = "",
    *,
    head_only: bool = False,
) -> bytes:
    head = [
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        *([extra] if extra else []),
        "Connection: close",
    ]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + (b"" if head_only else body)


def health_for(
    headers: Mapping[str, str], status: int, report: dict[str, Any], token: str | None
) -> tuple[int, dict[str, Any]]:
    """The full report only with `Authorization: Bearer <token>`; the peer address is no trust
    signal (a proxy in the same network namespace connects from loopback). Everyone else gets `ok`."""
    if token and _bearer_authorized(headers.get("authorization"), token):
        return status, report
    return status, {"ok": bool(report.get("ok"))}


async def start_web(
    port: int,
    *,
    health: Callable[[], tuple[int, dict[str, Any]]],
    owntracks: Callable[[bytes], None] | None = None,
    secret: str | None = None,
    host: str = "0.0.0.0",
    health_token: str | None = None,
    today: Callable[[], tuple[RuntimeView, bool]] | None = None,
    recent_alerts: Callable[[], tuple[SentAlert, ...]] = lambda: (),
    calendar_token: str | None = None,
    calendar: Callable[[], bytes] | None = None,
    delivery: NtfyProof | None = None,
) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                method, path, headers, body = await asyncio.wait_for(_read(reader), READ_TIMEOUT_S)
            except _Refused as e:
                writer.write(_response(e.status))
            else:
                if method in ("GET", "HEAD") and path.startswith(CALENDAR_SUBSCRIPTION):
                    supplied = path.removeprefix(CALENDAR_SUBSCRIPTION)
                    if (
                        calendar is None
                        or not calendar_token
                        or not hmac.compare_digest(
                            supplied.encode(), f"{calendar_token}.ics".encode()
                        )
                    ):
                        writer.write(_response(404, extra=TODAY_HEADERS))
                    else:
                        writer.write(
                            _response(
                                200,
                                calendar(),
                                "text/calendar; charset=utf-8",
                                TODAY_HEADERS,
                                head_only=method == "HEAD",
                            )
                        )
                elif method == "GET" and (path == MANIFEST_PATH or path in ICONS):
                    if not health_token or today is None:
                        writer.write(_response(404, extra=TODAY_HEADERS))
                    elif path == MANIFEST_PATH:
                        writer.write(
                            _response(
                                200,
                                json.dumps(MANIFEST).encode(),
                                "application/manifest+json",
                                TODAY_HEADERS,
                            )
                        )
                    else:
                        writer.write(
                            _response(200, ICONS[path].read_bytes(), "image/png", TODAY_HEADERS)
                        )
                elif method == "GET" and (
                    path in (TODAY_PATH, PAUSED_PATH)
                    or path.startswith(("/calendar/", "/preview/"))
                ):
                    if not health_token or today is None:
                        writer.write(_response(404, extra=TODAY_HEADERS))
                    elif not _today_authorized(headers.get("authorization"), health_token):
                        writer.write(
                            _response(
                                401,
                                b"Authentication required.",
                                "text/plain; charset=utf-8",
                                TODAY_HEADERS + '\r\nWWW-Authenticate: Basic realm="tcc-today"',
                            )
                        )
                    else:
                        view, confirmed = today()
                        if path in (TODAY_PATH, PAUSED_PATH):
                            writer.write(
                                _response(
                                    200,
                                    render_today(
                                        view,
                                        confirmed,
                                        paused=path == PAUSED_PATH,
                                        alerts=recent_alerts(),
                                    ).encode(),
                                    "text/html; charset=utf-8",
                                    TODAY_PAGE_HEADERS,
                                )
                            )
                        else:
                            preview = path.startswith("/preview/")
                            prefix, extension = (
                                ("preview", "html") if preview else ("calendar", "ics")
                            )
                            occurrence = next(
                                (
                                    o
                                    for o in view.occurrences
                                    if path == f"/{prefix}/{export_id(o.key)}.{extension}"
                                ),
                                None,
                            )
                            try:
                                if occurrence is None:
                                    raise CalendarExportError("unknown journey")
                                exported = (
                                    export_preview(occurrence, exported_at=view.at)
                                    if preview
                                    else export_ics(occurrence, exported_at=view.at)
                                )
                            except CalendarExportError:
                                writer.write(_response(404, extra=TODAY_HEADERS))
                            else:
                                filename = occurrence.key.service_date.isoformat() + "." + extension
                                writer.write(
                                    _response(
                                        200,
                                        exported,
                                        "text/html; charset=utf-8"
                                        if preview
                                        else "text/calendar; charset=utf-8",
                                        TODAY_HEADERS
                                        + f'\r\nContent-Disposition: attachment; filename="{filename}"',
                                    )
                                )
                elif method == "POST" and path in ("/delivery/test", "/delivery/confirm"):
                    if delivery is None or not health_token:
                        writer.write(_response(404))
                    elif not _bearer_authorized(headers.get("authorization"), health_token):
                        writer.write(_response(401))
                    else:
                        status, result = await (
                            delivery.test() if path == "/delivery/test" else delivery.confirm(body)
                        )
                        writer.write(
                            _response(
                                status, json.dumps(result).encode(), extra="Cache-Control: no-store"
                            )
                        )
                elif method == "GET" and path == "/health":
                    status, report = health_for(headers, *health(), health_token)
                    writer.write(
                        _response(
                            status, json.dumps(report).encode(), extra="Cache-Control: no-store"
                        )
                    )
                elif method == "POST" and path == "/owntracks" and owntracks and secret:
                    if not _authorized(headers.get("authorization"), secret):
                        writer.write(_response(401, extra='WWW-Authenticate: Basic realm="tcc"'))
                    else:
                        owntracks(body)  # parses and feeds; the core is not awaited
                        writer.write(_response(200, b"[]"))
                else:
                    writer.write(_response(404))
            await writer.drain()
        except TimeoutError:
            pass  # a client that never finished its request: just closed
        except Exception:
            log.exception("HTTP request failed")
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port, limit=HEAD_LIMIT)


def state_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("TCC_STATE_DIR", "").strip() or "/data")


def mcp_socket_path(env: Mapping[str, str]) -> Path:
    """In the state directory: the example compose file runs the container read-only, and the
    data volume is the one writable place."""
    return state_dir(env) / "mcp.sock"


async def start_readout(
    path: Path,
    answer: Callable[[object], dict[str, Any]],
    *,
    actions: Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]] | None = None,
) -> asyncio.Server | None:
    """Relay one JSON request. Existing calls without credentials use the read-only view."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(MCP_TIMEOUT_S) as deadline:
                line = await reader.readline()
                if not line.endswith(b"\n"):
                    return  # gone client, or an oversized line ended by the limit
                try:
                    request = json.loads(line)
                except ValueError:
                    request = None
                try:
                    if actions is not None and isinstance(request, dict) and "token" in request:
                        token = request.pop("token")
                        deadline.reschedule(
                            asyncio.get_running_loop().time() + MCP_ACTION_TIMEOUT_S
                        )
                        response = await actions(request, token if isinstance(token, str) else None)
                    else:
                        response = answer(request)
                    data = json.dumps(response).encode()
                # any failure gets the fixed reply; the log has the class and the frames, never
                # the text (it may carry view data)
                except Exception as e:  # noqa: BLE001
                    frames = traceback.extract_tb(e.__traceback__)
                    where = ", ".join(f"{f.filename}:{f.lineno}" for f in frames)
                    log.error("MCP read-out failed (%s) at %s", type(e).__name__, where)
                    data = json.dumps({"error": "Internal error; see the app log."}).encode()
                if len(data) > MCP_REPLY_MAX - 1:
                    data = json.dumps({"error": "Reply too large."}).encode()
                writer.write(data + b"\n")
                await writer.drain()
        except TimeoutError:
            writer.transport.abort()  # drops a reply nobody reads, so it cannot hold the buffer
        except (ValueError, ConnectionError):
            pass  # an oversized line, or a client that went away
        finally:
            writer.close()

    server = None
    try:
        limit = MCP_ACTION_MAX if actions is not None else MCP_REQUEST_MAX
        server = await asyncio.start_unix_server(handle, path, limit=limit)
        os.chmod(path, 0o600)
    except OSError as e:
        if server is not None:
            server.close()
        log.warning("MCP socket unavailable (%s); MCP is off", type(e).__name__)
        return None
    return server


async def ask_readout(path: Path, request: Mapping[str, Any]) -> dict[str, Any]:
    """The client side: raises OSError, TimeoutError or ValueError on failure."""
    timeout = MCP_ACTION_TIMEOUT_S + 1 if "token" in request else MCP_TIMEOUT_S
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_unix_connection(path, limit=MCP_REPLY_MAX)
        try:
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
    if not line.endswith(b"\n"):
        raise ValueError("no reply")
    reply = json.loads(line)
    if isinstance(reply, dict):
        return reply
    raise ValueError("reply is not an object")

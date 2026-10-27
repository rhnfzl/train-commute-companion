"""The HTTP side on a tiny asyncio server: `/health` for an uptime check (7.11, 7.12 rule 22) and
`POST /owntracks` for OwnTracks HTTP mode (8)."""

import asyncio
import base64
import binascii
import hmac
import json
import logging
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Any

from train_commute_companion.view import RuntimeView

log = logging.getLogger(__name__)

HEAD_LIMIT = 16 * 1024
BODY_LIMIT = 64 * 1024
READ_TIMEOUT_S = 10.0


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
    if int(length) > BODY_LIMIT:
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


def _response(
    status: int, body: bytes = b"", content_type: str = "application/json", extra: str = ""
) -> bytes:
    head = [
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        *([extra] if extra else []),
        "Connection: close",
    ]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def health_for(
    headers: Mapping[str, str], status: int, report: dict[str, Any], token: str | None
) -> tuple[int, dict[str, Any]]:
    """The full report only with `Authorization: Bearer <token>`; the peer address is no trust
    signal (a proxy in the same network namespace connects from loopback). Everyone else gets `ok`."""
    scheme, _, given = headers.get("authorization", "").partition(" ")
    if token and scheme.lower() == "bearer" and hmac.compare_digest(given.encode(), token.encode()):
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
) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                method, path, headers, body = await asyncio.wait_for(_read(reader), READ_TIMEOUT_S)
            except _Refused as e:
                writer.write(_response(e.status))
            else:
                if method == "GET" and path == "/health":
                    status, report = health_for(headers, *health(), health_token)
                    writer.write(_response(status, json.dumps(report).encode()))
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

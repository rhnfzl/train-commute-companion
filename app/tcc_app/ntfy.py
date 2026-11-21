"""Phone alerts through the traveller's own ntfy destination, with local delivery proof."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from train_commute_companion.model import AlertEvent, Severity, alert_facts
from train_commute_companion.notify import DestinationReservations, NotificationSuppressed
from train_commute_companion.webhook import check_token

from .bot_state import SetupClaim
from .telegram import BACKOFF, MessageProgress, PayloadPhase, split_text

log = logging.getLogger(__name__)
NTFY_DESTINATION_PREFIX = "ntfy:"


@dataclass(frozen=True, slots=True)
class NtfySettings:
    topic: str = field(repr=False)
    token: str = field(repr=False)
    server: str = field(default="https://ntfy.sh", repr=False)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{32,64}", self.topic) is None:
            raise ValueError(
                "TCC_NTFY_TOPIC must be a random 32-64 character topic (letters, digits, _ or -)"
            )
        try:
            check_token(self.token)
        except ValueError:
            raise ValueError(
                "TCC_NTFY_TOKEN must be 16+ printable ASCII characters without spaces"
            ) from None
        try:
            url = httpx.URL(self.server)
            safe = (
                url.scheme == "https"
                and bool(url.host)
                and not url.userinfo
                and url.raw_path in (b"", b"/")
                and not url.query
                and not url.fragment
                and (url.port is None or 1 <= url.port <= 65535)
                and not any(char.isspace() for char in self.server)
            )
        except (ValueError, httpx.InvalidURL):
            safe = False
        if not safe:
            raise ValueError(
                "TCC_NTFY_SERVER must be an HTTPS server root without credentials, query or fragment"
            )
        object.__setattr__(self, "server", str(url).rstrip("/"))

    @property
    def destination_id(self) -> str:
        # An authentication change also requires fresh proof and cannot inherit queued alerts.
        scope = json.dumps([self.server, self.topic]).encode()
        return (
            NTFY_DESTINATION_PREFIX
            + hmac.new(self.token.encode(), scope, hashlib.sha256).hexdigest()
        )


def ntfy_settings(env: Mapping[str, str]) -> NtfySettings | None:
    enabled = env.get("TCC_NTFY_ENABLED", "true").strip().lower()
    if enabled not in ("true", "false"):
        raise ValueError("TCC_NTFY_ENABLED must be true or false")
    values = [env.get(name, "") for name in ("TCC_NTFY_TOPIC", "TCC_NTFY_TOKEN", "TCC_NTFY_SERVER")]
    if not any(values):
        return None
    settings = NtfySettings(values[0], values[1], values[2] or "https://ntfy.sh")
    return settings if enabled == "true" else None


class NtfyNotifier:
    def __init__(
        self,
        settings: NtfySettings,
        client: httpx.AsyncClient,
        now: Callable[[], datetime],
        *,
        send_allowed: Callable[[AlertEvent], bool] = lambda event: True,
    ) -> None:
        self._settings, self._client, self._now = settings, client, now
        self._send_allowed = send_allowed
        self._reservations = DestinationReservations()
        self._sending = asyncio.Lock()
        self._retired = False
        self._parts: dict[tuple[str, str, str], MessageProgress] = {}

    @property
    def destination_id(self) -> str:
        return self._settings.destination_id

    def retire(self) -> None:
        self._retired = True

    async def publish(self, text: str, loud: bool) -> bool:
        destination = self.destination_id
        if self._retired or not self._reservations.reserve((destination,), self._now(), BACKOFF):
            return False
        accepted = False
        try:
            # JSON keeps the private topic out of URLs. Never follow redirects with the token.
            response = await self._client.post(
                self._settings.server + "/",
                headers={"Authorization": "Bearer " + self._settings.token},
                json={"topic": self._settings.topic, "message": text, "priority": 4 if loud else 1},
                timeout=10.0,
                follow_redirects=False,
            )
            accepted = response.is_success
            if not accepted:
                log.warning(
                    "ntfy refused an alert (HTTP %d); retry after 60 seconds", response.status_code
                )
        except httpx.HTTPError:
            log.warning("ntfy request failed; retry after 60 seconds")
        finally:
            self._reservations.settle(destination, accepted)
        return accepted

    async def send(self, event: AlertEvent) -> bool:
        async with self._sending:
            key = (event.profile_id, event.direction.value if event.direction else "", event.key)
            loud = event.severity is Severity.LOUD
            links = alert_facts(event).navigation or ()
            text = "\n".join((event.text, *links))
            payload = (text, not loud, "")
            # ntfy's limit is bytes. Each Python character needs at most four UTF-8 bytes.
            parts = split_text(text, limit=1000)
            progress = self._parts.get(key)
            if progress is None or progress.payload != payload:
                progress = MessageProgress(payload, len(parts))
            while progress.phase is not PayloadPhase.COMPLETE:
                if not self._send_allowed(event):
                    self._parts.pop(key, None)
                    raise NotificationSuppressed
                if not await self.publish(parts[progress.accepted], loud):
                    return False
                progress = progress.accept(progress.accepted)
                self._parts[key] = progress
            self._parts.pop(key, None)
            return True


class NtfyProof:
    """Two fresh phone-only codes, checked through the authenticated local setup endpoint."""

    def __init__(self, notifier: NtfyNotifier, path: Path, now: Callable[[], datetime]) -> None:
        self._notifier, self._path, self._now = notifier, path, now
        self._claims: tuple[SetupClaim, SetupClaim] | None = None
        self._lock = asyncio.Lock()
        self.writes: set[asyncio.Task[tuple[int, dict[str, object]]]] = set()
        self._confirmed_for: str | None = None
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("not a regular file")
            raw = json.loads(path.read_text())
            if isinstance(raw, dict) and isinstance(raw.get("confirmed_for"), str):
                self._confirmed_for = raw["confirmed_for"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("ntfy delivery proof is unreadable; repeat the local delivery test")

    @property
    def confirmed(self) -> bool:
        return self._confirmed_for == self._notifier.destination_id

    async def test(self) -> tuple[int, dict[str, object]]:
        async with self._lock:
            self._claims = None
            claims = tuple(SetupClaim(secrets.token_hex(8), self._now()) for _ in range(2))
            for claim, label, loud in zip(claims, ("loud", "quiet"), (True, False), strict=True):
                if not await self._notifier.publish(
                    f"Train Commute Companion: {label} phone test. Confirmation code: {claim.code}",
                    loud,
                ):
                    return 502, {"error": "ntfy test was not accepted. Retry after 60 seconds."}
            self._claims = claims[0], claims[1]
            return 200, {
                "accepted": True,
                "delivery_confirmed": self.confirmed,
                "message": "Read both codes on your phone, then confirm locally within 15 minutes.",
            }

    async def confirm(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            raw = json.loads(body)
            valid = (
                isinstance(raw, dict)
                and set(raw) == {"loud", "quiet"}
                and all(
                    isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value)
                    for value in raw.values()
                )
            )
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            return 400, {"error": "Supply the loud and quiet phone codes as JSON strings."}
        task = asyncio.create_task(self._confirm(raw))
        self.writes.add(task)
        task.add_done_callback(self.writes.discard)
        return await asyncio.shield(task)

    async def close(self) -> None:
        await asyncio.gather(*self.writes)

    async def _confirm(self, codes: dict[str, str]) -> tuple[int, dict[str, object]]:
        async with self._lock:
            if self._claims is None or not all(
                claim.accepts(codes[name], self._now())
                for claim, name in zip(self._claims, ("loud", "quiet"), strict=True)
            ):
                return 409, {"error": "Phone codes are wrong or expired. Repeat the delivery test."}
            if not await asyncio.to_thread(self._save):
                return 503, {"error": "Phone confirmation could not be saved. Retry confirmation."}
            self._confirmed_for = self._notifier.destination_id
            self._claims = None
            return 200, {"delivery_confirmed": True}

    def _save(self) -> bool:
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self._path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"confirmed_for": self._notifier.destination_id}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            return True
        except OSError:
            return False
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

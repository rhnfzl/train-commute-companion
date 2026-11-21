"""Deliver alerts from the connected runtime through the existing HA notifier."""

import asyncio
import json
from hashlib import sha256
from secrets import compare_digest

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from train_commute_companion.calendar import to_utc
from train_commute_companion.home_assistant_bridge import VERSION
from train_commute_companion.model import AlertEvent, EventKind
from train_commute_companion.notify import NotificationPaused
from train_commute_companion.serde import decode

from .const import CONF_REMOTE_TOKEN, DOMAIN
from .notifier import HaNotifier
from .remote import RemoteRuntime

MAX_ALERT_BYTES = 64 * 1024
MAX_RECEIPTS = 256


def _receipt_key(event: AlertEvent) -> str:
    identity = (
        event.profile_id,
        event.direction,
        event.key,
        event.severity,
        to_utc(event.created_at).isoformat(),
    )
    return sha256(json.dumps(identity).encode()).hexdigest()


class RemoteAlertView(HomeAssistantView):
    name = f"api:{DOMAIN}:bridge-alert"
    url = f"/api/{DOMAIN}/bridge-alert"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._receipt_lock = asyncio.Lock()
        self._inflight: dict[tuple[str, str], asyncio.Task[tuple[bool, bool] | None]] = {}
        self._sent_without_receipt: dict[tuple[str, str], None] = {}

    def _store(self, entry_id: str) -> Store[list[str]]:
        return Store(
            self._hass, 1, f"{DOMAIN}.bridge_receipts.{entry_id}", private=True, atomic_writes=True
        )

    async def _load(self, entry_id: str) -> list[str]:
        receipts = await self._store(entry_id).async_load()
        if receipts is None:
            return []
        if not isinstance(receipts, list) or any(not isinstance(key, str) for key in receipts):
            raise HomeAssistantError("Invalid Docker alert receipts.")
        return receipts[-MAX_RECEIPTS:]

    async def _deliver(
        self, notifier: HaNotifier, event: AlertEvent, key: tuple[str, str], destination: str
    ) -> tuple[bool, bool] | None:
        entry_id, digest = key
        try:
            async with self._receipt_lock:
                if digest in await self._load(entry_id):
                    self._sent_without_receipt.pop(key, None)
                    return True, False
            if key not in self._sent_without_receipt:
                if _destination(notifier, event) != destination:
                    return None
                if not await notifier.send(event):
                    return False, False
                self._sent_without_receipt[key] = None
                if len(self._sent_without_receipt) > MAX_RECEIPTS:
                    del self._sent_without_receipt[next(iter(self._sent_without_receipt))]
            async with self._receipt_lock:
                receipts = (await self._load(entry_id))[-(MAX_RECEIPTS - 1) :] + [digest]
                await self._store(entry_id).async_save(receipts)
                # HA Store logs certain write failures without raising. Confirm the file was saved.
                if await self._load(entry_id) != receipts:
                    return False, False
                self._sent_without_receipt.pop(key, None)
                return True, False
        except NotificationPaused:
            return False, True
        except (HomeAssistantError, OSError, KeyError, TypeError):
            return False, False

    def _authorized(self, request: web.Request) -> HaNotifier:
        entries = self._hass.config_entries.async_loaded_entries(DOMAIN)
        if len(entries) != 1:
            raise web.HTTPUnauthorized
        entry = entries[0]
        token = entry.options.get(CONF_REMOTE_TOKEN, "").strip()
        auth = request.headers.getall("Authorization", [])
        if (
            not isinstance(entry.runtime_data.host, RemoteRuntime)
            or not token
            or len(auth) != 1
            or not auth[0].isascii()
            or not compare_digest(auth[0].encode(), f"Bearer {token}".encode())
        ):
            raise web.HTTPUnauthorized
        notifier: HaNotifier = entry.runtime_data.notifier
        return notifier

    async def get(self, request: web.Request) -> web.Response:
        notifier = self._authorized(request)
        return self.json(
            {
                "version": VERSION,
                "destination": notifier.destination_id if notifier.available else None,
                "wake_destination": notifier.wake_destination if notifier.available else None,
                "wake_ready": await notifier.wake_ready(),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def post(self, request: web.Request) -> web.Response:
        notifier = self._authorized(request)
        try:
            async with asyncio.timeout(10):
                await request.content.readexactly(MAX_ALERT_BYTES + 1)
        except asyncio.IncompleteReadError as err:
            body = err.partial
        except TimeoutError:
            raise web.HTTPRequestTimeout from None
        else:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_ALERT_BYTES, actual_size=MAX_ALERT_BYTES + 1
            )
        try:
            message = json.loads(body)
            if (
                not isinstance(message, dict)
                or set(message) != {"version", "event"}
                or type(message["version"]) is not int
                or message["version"] != VERSION
            ):
                raise ValueError
            event = decode(AlertEvent, message["event"])
            key = notifier.entry_id, _receipt_key(event)
        except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
            raise web.HTTPBadRequest from None
        targets = request.headers.getall("X-TCC-Destination", [])
        if len(targets) != 1 or targets[0] != _destination(notifier, event):
            raise web.HTTPPreconditionFailed
        if (task := self._inflight.get(key)) is None:
            task = self._hass.async_create_task(
                self._deliver(notifier, event, key, targets[0]), "TCC Docker alert"
            )
            self._inflight[key] = task
            task.add_done_callback(lambda _: self._inflight.pop(key))
        # A disconnected Docker caller can retry while HA finishes and saves its receipt.
        result = await asyncio.shield(task)
        if result is None:
            raise web.HTTPPreconditionFailed
        accepted, paused = result
        return self.json({"accepted": accepted, "paused": paused})


def _destination(notifier: HaNotifier, event: AlertEvent) -> str | None:
    if not notifier.available:
        return None
    return (
        notifier.wake_destination if event.kind is EventKind.WAKE_ALARM else notifier.destination_id
    )

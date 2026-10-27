"""The HA delivery port: a bus event for automations, then a companion-app push."""

import asyncio
import logging
import threading
import time
from typing import Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from train_commute_companion.model import AlertEvent, Severity

from .const import (
    DEFAULT_LOUD_STYLE,
    DOMAIN,
    EVENT_ALERT,
    LOCAL_PUSH_SETTLE_S,
    LOUD_STYLES,
    NOTIFY_BACKOFF_S,
    NOTIFY_TIMEOUT_S,
)

_LOGGER = logging.getLogger(__name__)

MOBILE_APP = "mobile_app"
_MEMORY_CAP = 1000


def notify_payload(
    severity: Severity, text: str, loud_style: str = DEFAULT_LOUD_STYLE
) -> dict[str, Any]:
    if severity is Severity.SILENT:
        return {
            "message": text,
            "data": {
                "push": {"interruption-level": "passive"},
                "channel": "Train updates",
                "importance": "low",
            },
        }
    if loud_style == "time_sensitive":
        return {
            "message": text,
            "data": {
                "push": {"interruption-level": "time-sensitive", "sound": "default"},
                "ttl": 0,
                "priority": "high",
                "channel": "Train alerts",
                "importance": "high",
            },
        }
    if loud_style == "critical":
        return {
            "message": text,
            "data": {
                "push": {
                    "interruption-level": "critical",
                    "sound": {"name": "default", "critical": 1, "volume": 1.0},
                },
                "ttl": 0,
                "priority": "high",
                "channel": "alarm_stream",
            },
        }
    raise ValueError(f"unknown loud style {loud_style!r}, expected one of {LOUD_STYLES}")


def event_data(event: AlertEvent, entry_id: str, device_id: str | None) -> dict[str, Any]:
    t = event.train
    return {
        "config_entry_id": entry_id,
        "device_id": device_id,
        "profile_id": event.profile_id,
        "profile_name": event.profile_name,
        "direction": event.direction.value if event.direction else None,
        "service_date": event.service_date.isoformat() if event.service_date else None,
        "leg_index": event.leg_index,
        "kind": event.kind.value,
        "severity": event.severity.value,
        "train_operating_date": t.operating_date.isoformat() if t else None,
        "train_number": t.train_number if t else None,
        "train_departure": t.planned_departure.isoformat() if t else None,
        "text": event.text,
        "key": event.key,
        "created_at": event.created_at.isoformat(),
    }


def mobile_target(hass: HomeAssistant, service: str) -> str | None:
    """The webhook id HA's own mobile_app service would deliver to, from its service-to-target
    map, so colliding device-name slugs resolve to the same phone HA picks (the last one)."""
    # shortcut: reads mobile_app's internal hass.data layout (HA 2026.10.4), re-check on each HA bump
    data = hass.data.get(MOBILE_APP)
    if not isinstance(data, dict) or (svc := data.get("notify")) is None:
        return None
    target = getattr(svc, "registered_targets", {}).get(service)
    return target if target in data.get("config_entries", {}) else None


async def async_push(hass: HomeAssistant, service: str, payload: dict[str, Any]) -> bool:
    return (await _push(hass, service, payload))[0]


async def _push(
    hass: HomeAssistant,
    service: str,
    payload: dict[str, Any],
    retired: threading.Event | None = None,
    tasks: set[asyncio.Task[None]] | None = None,
) -> tuple[bool, bool]:
    """Returns (delivered, timed_out)."""
    # a service that is not a mobile_app phone proves nothing about delivery (7.12 rules 5, 16)
    if (webhook_id := mobile_target(hass, service)) is None:
        _LOGGER.warning("notify.%s is not a mobile_app phone; push not sent", service)
        return False, False
    try:
        return await _push_mobile(hass, webhook_id, payload, retired, tasks), False
    except (HomeAssistantError, TimeoutError) as err:
        _LOGGER.warning("push via notify.%s failed: %s", service, repr(err))
        return False, isinstance(err, TimeoutError)


async def _push_mobile(
    hass: HomeAssistant,
    webhook_id: str,
    payload: dict[str, Any],
    retired: threading.Event | None = None,
    tasks: set[asyncio.Task[None]] | None = None,
) -> bool:
    """mobile_app's own send path, minus its error swallowing: the relay's answer, or the local
    push confirmation, is observed directly; HA's service only logs a refused push. The relay
    fallback runs in a task this push owns: once the push stops waiting (deadline, cancel) its
    pending confirmation is withdrawn and the fallback cancelled, and a retired owner never
    relays."""
    # shortcut: calls mobile_app internals (HA 2026.10.4), re-check on each HA bump
    # imported here: only reached once mobile_app is loaded, which the manifest does not require
    from homeassistant.components.mobile_app import notify as mobile_notify

    data = hass.data[MOBILE_APP]
    entry = data["config_entries"][webhook_id]
    session = async_get_clientsession(hass)
    message = {"message": payload["message"], "data": payload.get("data")}
    channel: Any = data["push_channel"].get(webhook_id)  # PushChannel, untyped upstream
    if channel is None or not channel.support_confirm:
        # a local push with no confirmation proves nothing (HA drops it on a closing socket):
        # only the relay's answer counts (7.12 rules 5, 16)
        if "push_url" not in entry.data["app_data"]:
            raise HomeAssistantError(
                f"{entry.title} has no push relay and no confirming local push channel"
            )
        async with asyncio.timeout(NOTIFY_TIMEOUT_S):
            await mobile_notify._send_message(session, entry, message)
        return True

    done: asyncio.Future[bool] = hass.loop.create_future()

    def settle(ok: bool) -> None:
        if not done.done():
            done.set_result(ok)

    relays: set[asyncio.Task[None]] = set()

    async def relay(d: dict[str, Any]) -> None:
        ok = False
        try:
            if retired is not None and retired.is_set():
                return
            await mobile_notify._send_message(session, entry, d)
            ok = True
        except HomeAssistantError as err:
            _LOGGER.warning("remote fallback push to %s failed: %s", entry.title, repr(err))
        finally:
            settle(ok)

    async def fallback(d: dict[str, Any]) -> None:
        # HA calls this from its own timer or channel teardown, independent of our task
        if done.done() or (retired is not None and retired.is_set()):
            settle(False)
            return
        t = hass.async_create_task(relay(d))
        relays.add(t)
        if tasks is not None:
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    channel.async_send_notification(message, fallback)
    confirm_id = message.get("hass_confirm_id", "")
    pending = channel.pending_confirms.get(confirm_id)
    unsub: Any = None
    if pending is not None:
        # the phone's confirmation is the only caller of this unsubscribe (a failure pops first)
        unsub = pending["unsub_scheduled_push_failed"]

        def confirmed() -> None:
            unsub()
            settle(True)

        pending["unsub_scheduled_push_failed"] = confirmed
    try:
        # neither a confirmation nor a finished fallback by the deadline: unproven, so not delivered
        async with asyncio.timeout(LOCAL_PUSH_SETTLE_S):
            return await asyncio.shield(done)  # a cancel must not mark it settled
    except TimeoutError:
        _LOGGER.warning("local push to %s unconfirmed at the deadline", entry.title)
        return False
    finally:
        if not done.done():
            # nobody waits any more: HA must not relay this push later on our behalf
            settle(False)
            if channel.pending_confirms.pop(confirm_id, None) is not None and unsub is not None:
                unsub()
            for t in relays:
                t.cancel()


class HaNotifier:
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        service: str,
        loud_style: str = DEFAULT_LOUD_STYLE,
        retired: threading.Event | None = None,
    ) -> None:
        self.hass = hass
        # set once the host gives this runtime up: a runtime that outlived its stop never delivers
        self.retired = retired or threading.Event()
        self.entry_id = entry_id
        self.service = service
        self.loud_style = loud_style
        self._fired: set[tuple[str, Severity, Any]] = set()
        # this notifier's work on hass.loop (sends and relay fallbacks), cancelled on unload
        self._tasks: set[asyncio.Task[None]] = set()
        self._timed_out_at: float | None = None

    async def send(self, event: AlertEvent) -> bool:
        """Runs on the runtime thread; the work happens on `hass.loop`."""
        if self.retired.is_set():
            return False
        try:
            async with asyncio.timeout(NOTIFY_TIMEOUT_S + LOCAL_PUSH_SETTLE_S + 5):
                return await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(self._send(event), self.hass.loop)
                )
        except TimeoutError:
            return False

    async def async_stop(self) -> None:
        """On hass.loop, once `retired` is set: cancels and awaits this notifier's sends and
        fallbacks, so nothing reaches the phone after unload."""
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _send(self, event: AlertEvent) -> bool:
        if self.retired.is_set():
            return False
        me = cast("asyncio.Task[None]", asyncio.current_task())
        self._tasks.add(me)
        try:
            return await self._deliver(event)
        finally:
            self._tasks.discard(me)

    async def _deliver(self, event: AlertEvent) -> bool:
        fact = (event.key, event.severity, event.created_at)
        if fact not in self._fired:
            # shortcut: bounded by clearing, a repeat bus event after 1000 facts is harmless
            if len(self._fired) > _MEMORY_CAP:
                self._fired.clear()
            self._fired.add(fact)
            device = dr.async_get(self.hass).async_get_device(
                identifiers={(DOMAIN, event.profile_id)}
            )
            self.hass.bus.async_fire(
                EVENT_ALERT, event_data(event, self.entry_id, device.id if device else None)
            )
        now = time.monotonic()
        if self._timed_out_at is not None and now - self._timed_out_at < NOTIFY_BACKOFF_S:
            return False
        payload = notify_payload(event.severity, event.text, self.loud_style)
        ok, timed_out = await _push(self.hass, self.service, payload, self.retired, self._tasks)
        if timed_out:
            self._timed_out_at = now
        return ok

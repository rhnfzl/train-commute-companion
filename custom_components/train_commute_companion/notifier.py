"""The HA delivery port: a bus event for automations, then a companion-app push."""

import asyncio
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import slugify

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


def local_push_open(hass: HomeAssistant, service: str) -> bool:
    # shortcut: reads mobile_app's internal hass.data layout (HA 2026.10.4), re-check on each HA bump
    data = hass.data.get(MOBILE_APP)
    if not isinstance(data, dict):
        return False
    channels = data.get("push_channel", {})
    for webhook_id, entry in data.get("config_entries", {}).items():
        if slugify(f"{MOBILE_APP}_{entry.data.get('device_name', '')}") == service:
            channel = channels.get(webhook_id)
            return bool(channel is not None and channel.support_confirm)
    return False


class _Failures(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def async_push(hass: HomeAssistant, service: str, payload: dict[str, Any]) -> bool:
    return (await _push(hass, service, payload))[0]


async def _push(
    hass: HomeAssistant, service: str, payload: dict[str, Any]
) -> tuple[bool, bool]:
    """Returns (delivered, timed_out)."""
    # shortcut: any mobile_app warning during the window counts as a failure; an unrelated one
    # gives a false False (a retry, a duplicate push), the safe direction
    handler = _Failures()
    logger = logging.getLogger(f"homeassistant.components.{MOBILE_APP}")
    logger.addHandler(handler)
    try:
        async with asyncio.timeout(NOTIFY_TIMEOUT_S):
            await hass.services.async_call("notify", service, payload, blocking=True)
        if local_push_open(hass, service):
            await asyncio.sleep(LOCAL_PUSH_SETTLE_S)
    except (HomeAssistantError, TimeoutError) as err:
        _LOGGER.warning("push via notify.%s failed: %s", service, repr(err))
        return False, isinstance(err, TimeoutError)
    finally:
        logger.removeHandler(handler)
    if handler.records:
        _LOGGER.warning("push via notify.%s reported: %s", service, handler.records[0].getMessage())
        return False, False
    return True, False


class HaNotifier:
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        service: str,
        loud_style: str = DEFAULT_LOUD_STYLE,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.service = service
        self.loud_style = loud_style
        self._fired: set[tuple[str, Severity, Any]] = set()
        self._timed_out_at: float | None = None

    async def send(self, event: AlertEvent) -> bool:
        """Runs on the runtime thread; the work happens on `hass.loop`."""
        try:
            async with asyncio.timeout(NOTIFY_TIMEOUT_S + LOCAL_PUSH_SETTLE_S + 5):
                return await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(self._send(event), self.hass.loop)
                )
        except TimeoutError:
            return False

    async def _send(self, event: AlertEvent) -> bool:
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
        ok, timed_out = await _push(self.hass, self.service, payload)
        if timed_out:
            self._timed_out_at = now
        return ok

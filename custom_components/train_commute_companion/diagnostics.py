"""Download health information without revealing credentials or a travel routine."""

from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_LOUD_STYLE, CONF_WEBHOOK_URLS, DEFAULT_LOUD_STYLE
from .data import TccConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TccConfigEntry
) -> dict[str, Any]:
    data = getattr(entry, "runtime_data", None)
    view = data.coordinator.data if data is not None else None
    runtime: dict[str, Any] = {"available": False}
    if data is not None and view is not None:
        runtime = {
            "available": data.host.alive and data.coordinator.last_update_success,
            "started": view.started,
            "profile_count": len(view.profiles),
            "profile_refused": view.refusal is not None,
            "store_ok": view.store_ok,
            "key_rejected": view.key_rejected,
            "undelivered": view.undelivered,
            "notifications_paused": data.coordinator.notification_paused(),
            "notifications_quiet": data.coordinator.notification_quiet(),
            "requests": view.requests,
            "journeys": [
                {
                    "direction": o.key.direction.value,
                    "state": o.direction_state.value,
                    "resolved": o.resolved,
                    "leg_count": len(o.legs),
                    "transfer_count": len(o.transfers),
                    "fresh": o.health.fresh,
                    "consecutive_failures": o.health.consecutive_failures,
                    "auth_failed": o.health.auth_failed,
                }
                for o in view.occurrences
            ],
        }
    return {
        "schema_version": entry.version,
        "settings": {
            "loud_style": entry.options.get(CONF_LOUD_STYLE, DEFAULT_LOUD_STYLE),
            "webhooks_configured": bool(entry.options.get(CONF_WEBHOOK_URLS)),
        },
        "runtime": runtime,
    }

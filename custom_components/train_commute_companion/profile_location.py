"""Local entity choices for the selected person's setup."""

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

_ACTIVITY_IDS = ("activity", "detected_activity")
_ACTIVITIES = {"automotive", "in_vehicle", "stationary", "walking", "still", "on_foot"}


def _phone_devices(registry: er.EntityRegistry, person: State) -> set[str]:
    trackers = person.attributes.get("device_trackers")
    if not isinstance(trackers, (list, tuple)):
        return set()
    return {
        entry.device_id
        for entity in trackers
        if isinstance(entity, str)
        and (entry := registry.async_get(entity)) is not None
        and entry.domain == "device_tracker"
        and entry.platform == "mobile_app"
        and entry.device_id is not None
    }


def _mobile_activity(hass: HomeAssistant, entry: er.RegistryEntry) -> bool:
    if entry.domain != "sensor" or entry.platform != "mobile_app" or entry.config_entry_id is None:
        return False
    config = hass.config_entries.async_get_entry(entry.config_entry_id)
    webhook = config.data.get(CONF_WEBHOOK_ID) if config else None
    return bool(webhook) and any(entry.unique_id == f"{webhook}_{uid}" for uid in _ACTIVITY_IDS)


def _custom_activity(state: State) -> bool:
    options = state.attributes.get("options")
    return state.state.lower() in _ACTIVITIES or (
        isinstance(options, (list, tuple))
        and any(isinstance(value, str) and value.lower() in _ACTIVITIES for value in options)
    )


def _wifi(state: State) -> bool:
    return state.attributes.get("source_type") == "router"


def location_choices(
    hass: HomeAssistant,
    person: State,
    saved: Mapping[str, Any],
    values: Mapping[str, Any] | None,
) -> tuple[vol.Schema, dict[str, str], str]:
    registry = er.async_get(hass)
    entities = {
        "zone": {s.entity_id for s in hass.states.async_all("zone")},
        "device_tracker": {
            s.entity_id for s in hass.states.async_all("device_tracker") if _wifi(s)
        },
        "sensor": {
            entry.entity_id
            for entry in registry.entities.values()
            if entry.disabled_by is None and _mobile_activity(hass, entry)
        }
        | {s.entity_id for s in hass.states.async_all("sensor") if _custom_activity(s)},
    }
    defaults = {f"ha_{role}_zone": saved.get("zones", {}).get(role) for role in ("home", "work")}
    if saved.get("entity") == person.entity_id and saved.get("source") == "ha":
        for role in ("home", "work"):
            defaults[f"ha_{role}_wifi"] = saved.get("wifi", {}).get(role)
        defaults["ha_activity"] = saved.get("activity_entity")

    devices = _phone_devices(registry, person)
    wifi_hint = ""
    if len(devices) == 1 and not any(
        devices & _phone_devices(registry, other)
        for other in hass.states.async_all("person")
        if other.entity_id != person.entity_id
    ):
        device = next(iter(devices))
        phone_entities = er.async_entries_for_device(registry, device)
        wifi = [e.entity_id for e in phone_entities if e.entity_id in entities["device_tracker"]]
        if len(wifi) == 1:
            wifi_hint = wifi[0]  # the user chooses which zone the network serves
        activity = [e.entity_id for e in phone_entities if e.entity_id in entities["sensor"]]
        if len(activity) == 1 and "ha_activity" not in defaults:
            defaults["ha_activity"] = activity[0]

    fields: dict[Any, Any] = {}
    errors: dict[str, str] = {}
    for key, domain in {
        "ha_home_zone": "zone",
        "ha_work_zone": "zone",
        "ha_home_wifi": "device_tracker",
        "ha_work_wifi": "device_tracker",
        "ha_activity": "sensor",
    }.items():
        allowed = entities[domain]
        default = defaults.get(key)
        if default and default.startswith(f"{domain}."):
            allowed = allowed | {default}  # keep a saved entity if it is temporarily unavailable
        pick = defaults.get(key) if values is None else values.get(key)
        if pick and (not isinstance(pick, str) or pick not in allowed):
            if values is not None:
                errors[key] = "invalid_location_entity"
            pick = None
        if allowed:
            fields[vol.Optional(key, description={"suggested_value": pick})] = EntitySelector(
                EntitySelectorConfig(domain=domain, include_entities=sorted(allowed))
            )
    return vol.Schema(fields), errors, wifi_hint

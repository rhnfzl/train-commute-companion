"""Build a stock Home Assistant card for one commute direction."""

import json
from pathlib import Path

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.json import JsonArrayType

from train_commute_companion.model import Direction

from .const import DOMAIN
from .entity import unique_id
from .services import resolve_profile


async def _dashboard_card(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    data, profile_id, name = resolve_profile(hass, call.data["profile"])
    direction = call.data["direction"]
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, profile_id), data.coordinator.config_entry.entry_id
    )
    registry = er.async_get(hass)
    sensors = {
        key: registry.async_get_entity_id(
            "sensor", DOMAIN, unique_id(profile_id, key, None if key == "health" else direction)
        )
        for key in ("health", "status", "next_connection", "advice", "works_ahead")
    }
    if device is None or any(value is None for value in sensors.values()):
        raise ServiceValidationError(
            "The commute entities are unavailable. Reload the integration."
        )

    variables = {"profile_device": device.id, "direction": direction, "ids": sensors}
    settings = "\n".join(
        f"{{% set {key} = {json.dumps(value)} %}}" for key, value in variables.items()
    )
    template = await hass.async_add_executor_job(
        Path(__file__).with_name("dashboard.jinja").read_text
    )
    heading = "To work" if direction is Direction.OUTBOUND else "To home"
    cards: JsonArrayType = [
        {"type": "markdown", "title": f"{name} · {heading}", "content": f"{settings}\n{template}"}
    ]
    buttons: JsonArrayType = []
    for key, label in (
        ("leaving", "Leaving"),
        ("choose_next", "Choose next"),
        ("not_travelling", "Skip this trip"),
        ("not_travelling_today", "Skip today"),
    ):
        scope = None if key == "not_travelling_today" else direction
        entity = registry.async_get_entity_id("button", DOMAIN, unique_id(profile_id, key, scope))
        if entity is not None and hass.states.get(entity) is not None:
            buttons.append(
                {
                    "type": "button",
                    "entity": entity,
                    "name": label,
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "button.press",
                        "target": {"entity_id": entity},
                    },
                }
            )
    if buttons:
        cards.append({"type": "horizontal-stack", "cards": buttons})
    return {"type": "vertical-stack", "cards": cards}


def async_setup_dashboard_service(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        "dashboard_card",
        _dashboard_card,
        vol.Schema(
            {
                vol.Required("profile"): str,
                vol.Optional("direction", default=Direction.OUTBOUND): vol.Coerce(Direction),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

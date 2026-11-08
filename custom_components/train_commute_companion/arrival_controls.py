"""Explicit arrival services use the core's revision and durable-write guards."""

from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from train_commute_companion.compensation import arrival_text
from train_commute_companion.compensation_state import JourneyRecord
from train_commute_companion.messages import Refused
from train_commute_companion.model import Direction, OccurrenceKey
from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import encode

from .const import DOMAIN
from .data import TccData
from .services import _call_runtime, _refresh_or_refuse, resolve_profile


def aware_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise vol.Invalid("use a full ISO date and time with UTC offset") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise vol.Invalid("use a full ISO date and time with UTC offset")
    return value


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise vol.Invalid("revision must be a nonnegative whole number")
    return value


CONFIRM_ARRIVAL = vol.Schema(
    {
        vol.Required("profile"): cv.string,
        vol.Required("direction"): vol.Coerce(Direction),
        vol.Required("service_date"): cv.date,
        vol.Required("destination"): cv.string,
        vol.Required("arrived_at"): aware_timestamp,
        vol.Required("expected_revision"): _revision,
        vol.Required("action_id"): cv.string,
        vol.Optional("original_planned_arrival"): aware_timestamp,
    }
)


async def records(data: TccData) -> tuple[JourneyRecord, ...]:
    async def read(runtime: Runtime) -> tuple[JourneyRecord, ...]:
        return runtime.arrival_records()

    return await _call_runtime(data, read)


async def confirm_arrival(
    data: TccData,
    key: OccurrenceKey,
    destination: str,
    arrived_at: datetime,
    *,
    expected_revision: int,
    action_id: str,
    original_planned_arrival: datetime | None = None,
) -> JourneyRecord:
    result = await _call_runtime(
        data,
        lambda rt: rt.confirm_arrival(
            key,
            destination,
            arrived_at,
            expected_revision=expected_revision,
            action_id=action_id,
            original_planned_arrival=original_planned_arrival,
        ),
    )
    await _refresh_or_refuse(data, result if isinstance(result, Refused) else None)
    if isinstance(result, Refused):
        raise HomeAssistantError(result.text)
    return result


async def _confirm_arrival(call: ServiceCall) -> dict[str, Any]:
    entries = call.hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("The companion must be running to confirm an arrival")
    data: TccData = entries[0].runtime_data
    supplied = call.data["profile"]
    # Saved IDs remain exact identifiers after their profiles leave the active view.
    saved_ids = {record.key.profile_id for record in await records(data)}
    profile_id = supplied if supplied in saved_ids else resolve_profile(call.hass, supplied)[1]
    values = call.data
    key = OccurrenceKey(profile_id, values["direction"], values["service_date"])
    result = await confirm_arrival(
        data,
        key,
        values["destination"],
        values["arrived_at"],
        expected_revision=values["expected_revision"],
        action_id=values["action_id"],
        original_planned_arrival=values.get("original_planned_arrival"),
    )
    return {"record": encode(result), "text": arrival_text(result)}


def async_setup_arrival_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        "confirm_arrival",
        _confirm_arrival,
        CONFIRM_ARRIVAL,
        supports_response=SupportsResponse.OPTIONAL,
    )

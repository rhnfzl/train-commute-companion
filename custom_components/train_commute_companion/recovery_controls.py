"""Explicit recovery actions share the core's selection, revision and persistence checks."""

from datetime import date
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from train_commute_companion.messages import Refused
from train_commute_companion.model import Direction, OccurrenceKey
from train_commute_companion.recovery_state import RecoveryOffer, SelectedRecovery

from .const import DOMAIN
from .data import TccData
from .services import _call_runtime, _refresh_or_refuse, resolve_profile


def _whole(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise vol.Invalid("must be a nonnegative whole number")
    return value


BASE = {
    vol.Required("profile"): cv.string,
    vol.Required("direction"): vol.Coerce(Direction),
    vol.Required("service_date"): cv.date,
}
PREPARE = vol.Schema(
    {**BASE, vol.Required("proposal_id"): cv.string, vol.Required("origin_place_id"): cv.string}
)
FOLLOW = vol.Schema(
    {
        **BASE,
        vol.Required("offer_id"): cv.string,
        vol.Required("acknowledge_planned_times"): cv.boolean,
        vol.Required("acknowledge_accessibility_unknown"): cv.boolean,
    }
)
CONFIRM = vol.Schema(
    {
        **BASE,
        vol.Required("selection_id"): cv.string,
        vol.Required("revision"): _whole,
        vol.Required("step_index"): _whole,
        vol.Required("kind"): vol.In(("boarded", "arrived", "walk_arrived")),
        vol.Required("place_id"): cv.string,
        vol.Optional("action_id"): cv.string,
    }
)
STOP = vol.Schema(
    {**BASE, vol.Required("selection_id"): cv.string, vol.Required("revision"): _whole}
)


async def _accepted(
    data: TccData, result: RecoveryOffer | SelectedRecovery | Refused
) -> RecoveryOffer | SelectedRecovery:
    await _refresh_or_refuse(data, result if isinstance(result, Refused) else None)
    if isinstance(result, Refused):
        raise HomeAssistantError(result.text)
    return result


async def prepare(
    data: TccData, key: OccurrenceKey, proposal_id: str, origin_place_id: str
) -> RecoveryOffer:
    result = await _accepted(
        data,
        await _call_runtime(
            data, lambda rt: rt.prepare_recovery(key, proposal_id, origin_place_id)
        ),
    )
    if not isinstance(result, RecoveryOffer):
        raise HomeAssistantError("No recovery offer was prepared")
    return result


async def follow(
    data: TccData, key: OccurrenceKey, offer_id: str, *, planned: bool, accessibility_unknown: bool
) -> SelectedRecovery:
    result = await _accepted(
        data,
        await _call_runtime(
            data,
            lambda rt: rt.select_recovery(
                key,
                offer_id,
                acknowledge_planned_times=planned,
                acknowledge_accessibility_unknown=accessibility_unknown,
            ),
        ),
    )
    if not isinstance(result, SelectedRecovery):
        raise HomeAssistantError("Recovery was not selected")
    return result


async def confirm(
    data: TccData,
    key: OccurrenceKey,
    selection_id: str,
    revision: int,
    step_index: int,
    kind: str,
    place_id: str,
    action_id: str,
) -> SelectedRecovery:
    result = await _accepted(
        data,
        await _call_runtime(
            data,
            lambda rt: rt.confirm_recovery_step(
                key,
                selection_id,
                revision,
                step_index,
                kind,
                place_id,
                action_id,
            ),
        ),
    )
    if not isinstance(result, SelectedRecovery):
        raise HomeAssistantError("Recovery progress was not saved")
    return result


async def stop(
    data: TccData, key: OccurrenceKey, selection_id: str, revision: int
) -> SelectedRecovery:
    result = await _accepted(
        data, await _call_runtime(data, lambda rt: rt.stop_recovery(key, selection_id, revision))
    )
    if not isinstance(result, SelectedRecovery):
        raise HomeAssistantError("Recovery was not stopped")
    return result


def _target(call: ServiceCall) -> tuple[TccData, OccurrenceKey]:
    data, pid, _ = resolve_profile(call.hass, call.data["profile"])
    day: date = call.data["service_date"]
    return data, OccurrenceKey(pid, call.data["direction"], day)


async def _prepare(call: ServiceCall) -> dict[str, Any]:
    data, key = _target(call)
    offer = await prepare(data, key, call.data["proposal_id"], call.data["origin_place_id"])
    return offer.fields()


async def _follow(call: ServiceCall) -> dict[str, Any]:
    data, key = _target(call)
    result = await follow(
        data,
        key,
        call.data["offer_id"],
        planned=call.data["acknowledge_planned_times"],
        accessibility_unknown=call.data["acknowledge_accessibility_unknown"],
    )
    return result.fields(data.coordinator.data.at)


async def _confirm(call: ServiceCall) -> dict[str, Any]:
    data, key = _target(call)
    result = await confirm(
        data,
        key,
        call.data["selection_id"],
        call.data["revision"],
        call.data["step_index"],
        call.data["kind"],
        call.data["place_id"],
        call.data.get("action_id") or uuid4().hex,
    )
    return result.fields(data.coordinator.data.at)


async def _stop(call: ServiceCall) -> dict[str, Any]:
    data, key = _target(call)
    result = await stop(data, key, call.data["selection_id"], call.data["revision"])
    return result.fields(data.coordinator.data.at)


def async_setup_recovery_services(hass: HomeAssistant) -> None:
    for name, handler, schema in (
        ("prepare_recovery", _prepare, PREPARE),
        ("follow_recovery", _follow, FOLLOW),
        ("confirm_recovery_step", _confirm, CONFIRM),
        ("stop_recovery", _stop, STOP),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema, supports_response=SupportsResponse.OPTIONAL
        )

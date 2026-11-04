"""The three user actions (7.13) as services; the buttons call the same functions."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, time

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from train_commute_companion.messages import no_journey_text
from train_commute_companion.model import Direction, NotTravelling
from train_commute_companion.runtime import Runtime

from .const import DOMAIN
from .data import TccData
from .host import RuntimeNotRunning

# a choice holds one live request under the core's 90 s deadline
RUN_TIMEOUT_S = 120

BASE = {
    vol.Required("profile"): cv.string,
    vol.Optional("service_date"): cv.date,
    vol.Optional("direction"): vol.Coerce(Direction),
}
CHOOSE_SCHEMA = vol.Schema(
    {
        **BASE,
        vol.Required("departure"): cv.time,
        vol.Optional("train_number"): cv.string,
    }
)
LEAVING_SCHEMA = vol.Schema(BASE)
NOT_TRAVELLING_SCHEMA = vol.Schema({**BASE, vol.Optional("whole_day", default=False): cv.boolean})


def resolve(hass: HomeAssistant, profile: str) -> tuple[TccData, str, str]:
    """The loaded entry and the profile by id, then by name. Directions, not the file's profiles:
    a removed profile that still runs stays actionable (rule 10)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Train Commute Companion is not loaded")
    data: TccData = entries[0].runtime_data
    infos = data.coordinator.data.directions
    hit = next((i for i in infos if i.profile_id == profile), None) or next(
        (i for i in infos if i.profile_name == profile), None
    )
    if hit is None:
        names = sorted({i.profile_name for i in infos})
        raise ServiceValidationError(f"unknown profile {profile}; known: {', '.join(names)}")
    return data, hit.profile_id, hit.profile_name


async def _call_runtime[T](data: TccData, fn: Callable[[Runtime], Awaitable[T]]) -> T:
    try:
        async with asyncio.timeout(RUN_TIMEOUT_S):
            return await data.host.run(fn)
    except TimeoutError:
        raise HomeAssistantError("the companion is not answering; try again") from None
    except RuntimeNotRunning:
        raise HomeAssistantError("the companion is not running; try again") from None


async def do_choose(
    data: TccData,
    profile_id: str,
    departure: time,
    train_number: str | None,
    service_date: date | None,
    direction: Direction | None,
) -> None:
    reason = await _call_runtime(
        data,
        lambda rt: rt.choose_at(
            profile_id,
            departure,
            train_number=train_number,
            service_date=service_date,
            direction=direction,
        ),
    )
    await _refresh_or_refuse(data, reason)


async def _refresh_or_refuse(data: TccData, reason: str | None) -> None:
    await data.coordinator.async_request_refresh()
    if reason is not None:
        raise ServiceValidationError(reason)


def _refused_as(name: str, reason: str | None) -> str | None:
    """These two refusals name the profile as configured, not in the core's display form."""
    return None if reason is None else no_journey_text(name)


async def do_leaving(
    data: TccData,
    profile_id: str,
    name: str,
    service_date: date | None,
    direction: Direction | None,
) -> None:
    reason = await _call_runtime(data, lambda rt: rt.leaving(profile_id, service_date, direction))
    await _refresh_or_refuse(data, _refused_as(name, reason))


async def do_not_travelling(
    data: TccData,
    profile_id: str,
    name: str,
    service_date: date | None,
    direction: Direction | None,
    whole_day: bool,
) -> None:
    reason = await _call_runtime(
        data, lambda rt: rt.act(profile_id, NotTravelling(service_date, whole_day), direction)
    )
    await _refresh_or_refuse(data, _refused_as(name, reason))


async def _handle_choose(call: ServiceCall) -> None:
    d = call.data
    data, pid, _ = resolve(call.hass, d["profile"])
    await do_choose(
        data, pid, d["departure"], d.get("train_number"), d.get("service_date"), d.get("direction")
    )


async def _handle_leaving(call: ServiceCall) -> None:
    d = call.data
    data, pid, name = resolve(call.hass, d["profile"])
    await do_leaving(data, pid, name, d.get("service_date"), d.get("direction"))


async def _handle_not_travelling(call: ServiceCall) -> None:
    d = call.data
    data, pid, name = resolve(call.hass, d["profile"])
    await do_not_travelling(
        data, pid, name, d.get("service_date"), d.get("direction"), d["whole_day"]
    )


def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(DOMAIN, "choose", _handle_choose, CHOOSE_SCHEMA)
    hass.services.async_register(DOMAIN, "leaving", _handle_leaving, LEAVING_SCHEMA)
    hass.services.async_register(
        DOMAIN, "not_travelling", _handle_not_travelling, NOT_TRAVELLING_SCHEMA
    )

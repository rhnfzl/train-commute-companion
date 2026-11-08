"""The three user actions (7.13) as services; the buttons call the same functions."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, time
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from train_commute_companion.messages import RefusalKind, Refused, no_journey_text
from train_commute_companion.model import Direction
from train_commute_companion.runtime import Runtime
from train_commute_companion.tasks import DetachedCall

from .const import DOMAIN
from .data import TccData
from .host import RuntimeNotRunning

# a choice holds one live request under the core's 90 s deadline
RUN_TIMEOUT_S = 120
# calls that outlived their caller: the loop keeps only a weak reference to a task
_running: set[asyncio.Future[Any]] = set()
_LOGGER = logging.getLogger(__name__)

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


def resolve_profile(hass: HomeAssistant, profile: str) -> tuple[TccData, str, str]:
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
    """The runtime call is its own task: a timeout or a cancelled caller leaves it to finish (a
    cancelled delivery pass after a send would repeat that alert). `RuntimeHost.stop` ends it."""
    task = asyncio.ensure_future(data.host.run(fn))
    _running.add(task)
    task.add_done_callback(_running.discard)
    call = DetachedCall(
        task,
        lambda error: _LOGGER.error(
            "command failed after its caller stopped waiting", exc_info=error
        ),
    )
    try:
        # Keep the deadline on HA's clock; the shared owner tracks caller detachment.
        async with asyncio.timeout(RUN_TIMEOUT_S):
            return await call.wait(None)
    except TimeoutError:
        raise HomeAssistantError("the companion is not answering; try again") from None
    except RuntimeNotRunning:
        raise HomeAssistantError("the companion is not running; try again") from None


async def do_choose(
    data: TccData,
    profile_id: str,
    departure: time,
    *,
    train_number: str | None = None,
    service_date: date | None = None,
    direction: Direction | None = None,
) -> None:
    refused = await _call_runtime(
        data,
        lambda rt: rt.choose_at(
            profile_id,
            departure,
            train_number=train_number,
            service_date=service_date,
            direction=direction,
        ),
    )
    await _refresh_or_refuse(data, refused)


async def _refresh_or_refuse(
    data: TccData, refused: Refused | None, name: str | None = None
) -> None:
    """A try-again is no invalid input. `name`: no journey is told by the profile as configured,
    not in the core's display form."""
    await data.coordinator.async_request_refresh()
    if refused is None:
        return
    if refused.kind is RefusalKind.RETRY:
        raise HomeAssistantError(refused.text)
    if name is not None and refused.kind is RefusalKind.NO_JOURNEY:
        raise ServiceValidationError(no_journey_text(name))
    raise ServiceValidationError(refused.text)


async def do_leaving(
    data: TccData,
    profile_id: str,
    name: str,
    *,
    service_date: date | None = None,
    direction: Direction | None = None,
) -> None:
    refused = await _call_runtime(data, lambda rt: rt.leaving(profile_id, service_date, direction))
    await _refresh_or_refuse(data, refused, name)


async def do_not_travelling(
    data: TccData,
    profile_id: str,
    name: str,
    *,
    service_date: date | None = None,
    direction: Direction | None = None,
    whole_day: bool = False,
) -> None:
    refused = await _call_runtime(
        data, lambda rt: rt.not_travelling(profile_id, service_date, direction, whole_day=whole_day)
    )
    await _refresh_or_refuse(data, refused, name)


async def _handle_choose(call: ServiceCall) -> None:
    d = call.data
    data, pid, _ = resolve_profile(call.hass, d["profile"])
    await do_choose(
        data,
        pid,
        d["departure"],
        train_number=d.get("train_number"),
        service_date=d.get("service_date"),
        direction=d.get("direction"),
    )


async def _handle_leaving(call: ServiceCall) -> None:
    d = call.data
    data, pid, name = resolve_profile(call.hass, d["profile"])
    await do_leaving(
        data, pid, name, service_date=d.get("service_date"), direction=d.get("direction")
    )


async def _handle_not_travelling(call: ServiceCall) -> None:
    d = call.data
    data, pid, name = resolve_profile(call.hass, d["profile"])
    await do_not_travelling(
        data,
        pid,
        name,
        service_date=d.get("service_date"),
        direction=d.get("direction"),
        whole_day=d["whole_day"],
    )


def async_setup_services(hass: HomeAssistant) -> None:
    from .arrival_controls import async_setup_arrival_services
    from .recovery_controls import async_setup_recovery_services

    async_setup_arrival_services(hass)
    async_setup_recovery_services(hass)
    hass.services.async_register(DOMAIN, "choose", _handle_choose, CHOOSE_SCHEMA)
    hass.services.async_register(DOMAIN, "leaving", _handle_leaving, LEAVING_SCHEMA)
    hass.services.async_register(
        DOMAIN, "not_travelling", _handle_not_travelling, NOT_TRAVELLING_SCHEMA
    )

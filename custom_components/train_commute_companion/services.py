"""The user actions as services; the buttons call the same functions."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, time
from functools import partial
from pathlib import Path
from typing import Any, cast

import voluptuous as vol
import yaml
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from train_commute_companion.messages import RefusalKind, Refused, no_journey_text
from train_commute_companion.model import Direction
from train_commute_companion.runtime import Runtime
from train_commute_companion.tasks import DetachedCall

from .const import CONF_PROFILES_PATH, CONF_REMOTE_PROFILE_SYNC, DOMAIN
from .data import TccData
from .host import RuntimeHost, RuntimeNotRunning
from .remote import RemoteRuntime, RemoteUnavailable, invoke
from .validate import check_path

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
CHECK_TRIP_SCHEMA = vol.Schema({vol.Required("origin"): str, vol.Required("destination"): str})
CHECK_COMMUTE_SCHEMA = vol.Schema(
    {
        vol.Required("profile"): cv.string,
        vol.Required("direction"): vol.Coerce(Direction),
        vol.Required("date"): cv.date,
    }
)
NOT_TRAVELLING_SCHEMA = vol.Schema({**BASE, vol.Optional("whole_day", default=False): cv.boolean})
ANSWER_PROMPT_SCHEMA = vol.Schema(
    {vol.Required("prompt_id"): cv.string, vol.Required("answer"): cv.string}
)
FORGET_STATION_SCHEMA = vol.Schema(
    {
        vol.Required("profile"): cv.string,
        vol.Required("direction"): vol.Coerce(Direction),
        vol.Required("station"): cv.string,
    }
)


def _buffer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise vol.Invalid("must be a nonnegative whole number")
    return value


SCHEDULE_FIELDS = vol.Schema(
    {
        vol.Required("extra_dates"): [cv.date],
        vol.Required("outbound_buffer_min"): _buffer,
        vol.Required("return_buffer_min"): _buffer,
    }
)
SYNC_SCHEDULE_SCHEMA = SCHEDULE_FIELDS.extend({vol.Required("profile"): cv.string})


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
    if not isinstance(data.host, RuntimeHost):
        raise ServiceValidationError("This action requires a runtime managed by Home Assistant.")
    return await _await_action(data.host.run(fn))


async def _await_action[T](action: Awaitable[T]) -> T:
    """The runtime call is its own task: a timeout or a cancelled caller leaves it to finish (a
    cancelled delivery pass after a send would repeat that alert). `RuntimeHost.stop` ends it."""
    task = asyncio.ensure_future(action)
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
    except RemoteUnavailable as err:
        raise HomeAssistantError(str(err)) from None
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
    refused = await _await_action(
        invoke(
            data.host,
            Runtime.choose_at,
            profile_id=profile_id,
            departure=departure,
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
    refused = await _await_action(
        invoke(
            data.host,
            Runtime.leaving,
            profile_id=profile_id,
            service_date=service_date,
            direction=direction,
        )
    )
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
    refused = await _await_action(
        invoke(
            data.host,
            Runtime.not_travelling,
            profile_id=profile_id,
            service_date=service_date,
            direction=direction,
            whole_day=whole_day,
        )
    )
    await _refresh_or_refuse(data, refused, name)


async def do_travelling(
    data: TccData,
    profile_id: str,
    name: str,
    *,
    service_date: date | None = None,
    direction: Direction | None = None,
    whole_day: bool = False,
) -> None:
    refused = await _await_action(
        invoke(
            data.host,
            Runtime.travelling,
            profile_id=profile_id,
            service_date=service_date,
            direction=direction,
            whole_day=whole_day,
        )
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


async def _handle_travelling(call: ServiceCall) -> None:
    d = call.data
    data, pid, name = resolve_profile(call.hass, d["profile"])
    await do_travelling(
        data,
        pid,
        name,
        service_date=d.get("service_date"),
        direction=d.get("direction"),
        whole_day=d["whole_day"],
    )


async def do_answer_prompt(data: TccData, prompt_id: str, answer: str) -> None:
    """A button on a question about an other boarding station; the id finds the journey."""
    refused = await _await_action(
        invoke(
            data.host,
            Runtime.answer_prompt,
            prompt_id=prompt_id,
            answer=answer,
        )
    )
    await _refresh_or_refuse(data, refused)


def loaded(hass: HomeAssistant) -> TccData:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Train Commute Companion is not loaded")
    data: TccData = entries[0].runtime_data
    return data


async def _handle_check_trip(call: ServiceCall) -> ServiceResponse:
    data = loaded(call.hass)
    origin, destination = call.data["origin"], call.data["destination"]
    result = await _await_action(
        invoke(data.host, Runtime.check_trip, origin=origin, destination=destination)
    )
    if isinstance(result, Refused):
        if result.kind is RefusalKind.RETRY:
            raise HomeAssistantError(result.text)
        raise ServiceValidationError(result.text)
    return cast(ServiceResponse, result)


async def _handle_check_commute(call: ServiceCall) -> ServiceResponse:
    request = call.data
    data, profile_id, _ = resolve_profile(call.hass, request["profile"])
    result = await _await_action(
        invoke(
            data.host,
            Runtime.check_commute,
            profile_id=profile_id,
            direction=request["direction"],
            day=request["date"],
        )
    )
    return cast(ServiceResponse, result)


async def _handle_answer_prompt(call: ServiceCall) -> None:
    d = call.data
    await do_answer_prompt(loaded(call.hass), d["prompt_id"], d["answer"])


async def _handle_forget_station(call: ServiceCall) -> None:
    d = call.data
    data, pid, _ = resolve_profile(call.hass, d["profile"])
    station, direction = d["station"].strip().upper(), d["direction"]
    refused = await _await_action(
        invoke(
            data.host,
            Runtime.forget_station,
            profile_id=pid,
            direction=direction,
            station=station,
        )
    )
    await _refresh_or_refuse(data, refused)


MOBILE_ACTION = "mobile_app_notification_action"


async def _on_mobile_action(hass: HomeAssistant, event: Event) -> None:
    """A companion app button on a question; anything else, or a stale answer, is logged."""
    from .notifier import parse_answer_action

    parsed = parse_answer_action(event.data.get("action"))
    if parsed is None:
        return
    try:
        await do_answer_prompt(loaded(hass), *parsed)
    except HomeAssistantError as err:
        _LOGGER.info("answer from the phone not taken: %s", err)


async def _handle_reload_profiles(call: ServiceCall) -> None:
    data = loaded(call.hass)
    if isinstance(data.host, RuntimeHost):
        refused = await _call_runtime(data, lambda rt: rt.reload())
        await _refresh_or_refuse(data, refused)
        return
    options = data.coordinator.config_entry.options
    if not options.get(CONF_REMOTE_PROFILE_SYNC):
        raise ServiceValidationError(
            "Enable schedule synchronization or edit profiles on the Docker companion."
        )
    path = options.get(CONF_PROFILES_PATH, call.hass.config.path(DOMAIN, "profiles.yaml"))
    if await check_path(call.hass, path) is not None:
        raise ServiceValidationError("Home Assistant cannot read the local profiles file.")
    shared_ids = {p.id for p in data.coordinator.data.profiles}
    schedules = await call.hass.async_add_executor_job(_read_schedules, Path(path), shared_ids)
    for pid, values in schedules.items():
        await _sync_schedule(data, pid, values)


def _read_schedules(path: Path, shared_ids: set[str]) -> dict[str, dict[str, Any]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        schedules = {
            p["id"]: SCHEDULE_FIELDS(
                {
                    "extra_dates": p.get("extra_dates", []),
                    "outbound_buffer_min": p["outbound"]["buffer_min"],
                    "return_buffer_min": p["return"]["buffer_min"],
                }
            )
            for p in raw["profiles"]
            if p["id"] in shared_ids
        }
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        TypeError,
        KeyError,
        AttributeError,
        vol.Invalid,
    ):
        raise ServiceValidationError("The local profiles file has an invalid schedule.") from None
    if not schedules:
        raise ServiceValidationError("No local profile ID matches a Docker profile.")
    return schedules


async def _sync_schedule(data: TccData, pid: str, values: dict[str, Any]) -> None:
    if not isinstance(data.host, RemoteRuntime):
        raise ServiceValidationError(
            "Schedule synchronization requires a connected Docker runtime."
        )
    refused = await _await_action(
        invoke(
            data.host,
            Runtime.sync_profile,
            profile_id=pid,
            extra_dates=tuple(values["extra_dates"]),
            outbound_buffer_min=values["outbound_buffer_min"],
            return_buffer_min=values["return_buffer_min"],
        )
    )
    await _refresh_or_refuse(data, refused)


async def _handle_sync_schedule(call: ServiceCall) -> None:
    data, pid, _ = resolve_profile(call.hass, call.data["profile"])
    await _sync_schedule(data, pid, dict(call.data))


async def _handle_clear_commute_history(call: ServiceCall) -> None:
    data = loaded(call.hass)
    if not await _await_action(invoke(data.host, Runtime.clear_commute_history)):
        raise HomeAssistantError("Local commute history could not be cleared. Try again.")
    await data.coordinator.async_request_refresh()


def async_setup_services(hass: HomeAssistant) -> None:
    from .arrival_controls import async_setup_arrival_services
    from .dashboard import async_setup_dashboard_service
    from .recovery_controls import async_setup_recovery_services

    async_setup_arrival_services(hass)
    async_setup_dashboard_service(hass)
    async_setup_recovery_services(hass)
    hass.services.async_register(DOMAIN, "choose", _handle_choose, CHOOSE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        "check_trip",
        _handle_check_trip,
        CHECK_TRIP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "check_commute",
        _handle_check_commute,
        CHECK_COMMUTE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, "leaving", _handle_leaving, LEAVING_SCHEMA)
    hass.services.async_register(
        DOMAIN, "not_travelling", _handle_not_travelling, NOT_TRAVELLING_SCHEMA
    )
    hass.services.async_register(DOMAIN, "travelling", _handle_travelling, NOT_TRAVELLING_SCHEMA)
    hass.services.async_register(DOMAIN, "reload_profiles", _handle_reload_profiles, vol.Schema({}))
    async_register_admin_service(
        hass, DOMAIN, "sync_schedule", _handle_sync_schedule, SYNC_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "answer_prompt", _handle_answer_prompt, ANSWER_PROMPT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "forget_station", _handle_forget_station, FORGET_STATION_SCHEMA
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        "clear_commute_history",
        _handle_clear_commute_history,
        vol.Schema({vol.Required("confirm"): vol.All(bool, vol.In([True]))}),
    )
    hass.bus.async_listen(MOBILE_ACTION, partial(_on_mobile_action, hass))

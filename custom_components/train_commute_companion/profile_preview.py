"""A bounded timetable preview of a validated profile, before it is saved."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from train_commute_companion.advice import is_unrideable
from train_commute_companion.calendar import TZ, to_utc
from train_commute_companion.classify import current_connection, evidence
from train_commute_companion.location import distance_m, number
from train_commute_companion.lookahead_probe import COMMUTE_DATE_SEARCH_DAYS
from train_commute_companion.match import current_arrival, planned_departure, usual_connection
from train_commute_companion.model import DatedSchedule, Direction, Profile, Stations
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.replacement import max_transfers
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.state import PollFailure

PREVIEW_TIMEOUT_S = 5


def nearby_stations(
    hass: HomeAssistant, catalogue: Stations, codes: Mapping[str, str]
) -> list[str]:
    home = hass.states.get("zone.home")
    if home is None or home.state in ("unknown", "unavailable"):
        return []
    lat = number(home.attributes.get("latitude"))
    lon = number(home.attributes.get("longitude"))
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return []
    distances = []
    for label, code in codes.items():
        pos = catalogue.positions.get(code)
        if pos is not None and abs(pos[0]) <= 90 and abs(pos[1]) <= 180:
            distances.append((distance_m(lat, lon, *pos), label))
    return [label for _, label in sorted(distances)[:3]]


def _upcoming(profile: Profile, start: datetime) -> Iterator[tuple[datetime, DatedSchedule]]:
    today = start.astimezone(TZ).date()
    now = to_utc(start)
    for offset in range(-2, COMMUTE_DATE_SEARCH_DAYS + 1):
        day = today + timedelta(days=offset)
        if not profile.commutes_on(day):
            continue
        for direction in Direction:
            schedule = dated_schedule(profile, direction, day, None)
            if profile.paused_on(schedule.key.service_date):
                continue
            first = to_utc(schedule.first_departure)
            if schedule.last_departure is not None:
                first = max(first, now)
                if first <= to_utc(schedule.last_departure):
                    yield first, schedule
            elif first >= now:
                yield first, schedule


async def trip_preview(
    profile: Profile,
    stations: Stations,
    now: Callable[[], datetime],
    fetch: Callable[[Query], Awaitable[ProviderAnswer | PollFailure | None]],
) -> dict[str, str] | None:
    start = now()
    next_trip = min(_upcoming(profile, start), key=lambda pair: pair[0], default=None)
    if next_trip is None:
        return None
    first, schedule = next_trip
    direction = schedule.key.direction
    origin, destination = profile.origin(direction), profile.destination(direction)
    transfers = profile.journey(direction).transfers
    via = next(iter(transfers), None) if schedule.mode == "fixed" else None
    query = Query.for_profile(profile, origin, destination, first.astimezone(TZ), via)
    try:
        async with asyncio.timeout(PREVIEW_TIMEOUT_S):
            answer = await fetch(query)
    except TimeoutError:
        return None
    if answer is None or isinstance(answer, str):
        return None
    fetched = answer.fetched_at
    if (
        answer.query != query
        or fetched.utcoffset() is None
        or not to_utc(start) <= to_utc(fetched) <= to_utc(now())
    ):
        return None
    profile_min = dict(profile.transfer_min)
    deadline = schedule.arrival_deadline
    usable = tuple(
        c
        for c in answer.connections
        if c.origin == origin
        and c.destination == destination
        and c.status not in ("CANCELLED", "MAINTENANCE")
        and all(leg.reachable is not False for leg in c.legs)
        and not is_unrideable(c, answer, profile_min)
        and (
            deadline is None
            or (arrival := c.legs[-1].alight.planned_arrival) is not None
            and to_utc(arrival) <= deadline
            and current_arrival(answer, c) <= deadline
        )
    )
    if schedule.last_departure is None:
        trip = usual_connection(replace(answer, connections=usable), origin, first, transfers, None)
    else:
        latest = to_utc(schedule.last_departure)
        # A dated timetable can have planned times only. Apply the shared rideability checks
        # without the live replacement rule that requires reported times for every leg.
        trip = min(
            (
                c
                for c in usable
                if first <= planned_departure(c.legs[0]) <= latest
                and len(c.transfer_stations) <= max_transfers(transfers)
            ),
            key=lambda c: (planned_departure(c.legs[0]), current_arrival(answer, c)),
            default=None,
        )
    if trip is None:
        return None
    board = current_connection(evidence(answer), trip).legs[0].board
    return {
        "departure": planned_departure(trip.legs[0]).astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        "platform": board.actual_track or board.planned_track or "?",
        "route": " → ".join(
            stations.name(code) for code in (origin, *trip.transfer_stations, destination)
        ),
    }

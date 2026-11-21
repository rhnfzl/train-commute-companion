"""Read-only selection and presentation of upcoming NS train itineraries."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from train_commute_companion.calendar import TZ, hhmm, to_utc
from train_commute_companion.classify import classify_leg, current_connection
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.messages import RefusalKind, Refused
from train_commute_companion.model import (
    Connection,
    Leg,
    LegKind,
    Stations,
    Stop,
    TimeObs,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.transfer import check_transfer, is_reported


def resolve_station(catalogue: Stations, value: str) -> str | Refused:
    value = value.strip()
    if not value or len(value) > 80 or not value.isprintable():
        return Refused("Enter an NS station code or name of up to 80 characters.")
    code = value.upper()
    matches = (code,) if code in catalogue else catalogue.lookup(value)
    if len(matches) == 1:
        return matches[0]
    if matches:
        choices = ", ".join(f"{c} ({catalogue.name(c)})" for c in sorted(matches))
        return Refused(f"{value} matches several stations: {choices}. Use a station code.")
    return Refused(f"Unknown NS station: {value}. Use its full name or station code.")


def _usable(connection: Connection) -> bool:
    if connection.status not in ("NORMAL", "ALTERNATIVE_TRANSPORT"):
        return False
    legs = connection.legs
    if any(
        not leg.train.train_number
        or len(leg.stops) < 2
        or leg.alternative_transport
        or leg.reachable is False
        for leg in legs
    ):
        return False
    classified = tuple(classify_leg(leg, leg.alight.code) for leg in legs)
    if any(leg.kind in (LegKind.CANCELLED, LegKind.CUT_SHORT) for leg in classified):
        return False
    return all(
        a.leg.alight.code == b.leg.board.code
        and check_transfer(a, b, b.leg.board.code, {}).state is not TransferState.BROKEN
        for a, b in pairwise(classified)
    )


def _expected(scheduled: datetime | None, observation: TimeObs | None) -> datetime | None:
    return (observation.value if observation and is_reported(observation) else None) or scheduled


def _departure(leg: Leg) -> datetime:
    scheduled = leg.board.planned_departure or leg.train.planned_departure
    return to_utc(_expected(scheduled, leg.board.departure) or scheduled)


def _arrival(leg: Leg) -> datetime | None:
    arrival = _expected(leg.alight.planned_arrival, leg.alight.arrival)
    return to_utc(arrival) if arrival is not None else None


def _station(catalogue: Stations, stop: Stop) -> dict[str, str]:
    return {"code": stop.code, "name": next(iter(catalogue.labels.get(stop.code, ())), stop.name)}


def _time(
    scheduled: datetime | None, observation: TimeObs | None, checked_at: datetime
) -> tuple[dict[str, str | None], str]:
    reported = observation.value if observation and is_reported(observation) else None

    def display(instant: datetime) -> str:
        local = instant.astimezone(TZ)
        return hhmm(local) if local.date() == checked_at.date() else f"{local:%d %b %H:%M}"

    times = {"scheduled": scheduled, "reported": reported}
    text = ", ".join(f"{display(t)} {label}" for label, t in times.items() if t is not None)
    return {label: t.isoformat() if t is not None else None for label, t in times.items()}, (
        text or "unavailable"
    )


def _leg(leg: Leg, catalogue: Stations, checked_at: datetime) -> tuple[dict[str, Any], str]:
    board, alight = leg.board, leg.alight
    origin, destination = _station(catalogue, board), _station(catalogue, alight)
    departure, dep_text = _time(
        board.planned_departure or leg.train.planned_departure, board.departure, checked_at
    )
    arrival, arr_text = _time(alight.planned_arrival, alight.arrival, checked_at)
    train = " ".join(filter(None, (leg.category, leg.train.train_number)))
    text = (
        f"{train} {origin['name']} to {destination['name']}. "
        f"Departs {dep_text}. Arrives {arr_text}."
    )
    if board.actual_track:
        text += f" Platform {board.actual_track} reported."
    elif board.planned_track:
        text += f" Platform {board.planned_track} scheduled."
    if leg.crowd_forecast:
        text += f" Crowd forecast: {leg.crowd_forecast}."
    return {
        "train_number": leg.train.train_number,
        "category": leg.category,
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "platform": {"scheduled": board.planned_track, "reported": board.actual_track},
        "crowd_forecast": leg.crowd_forecast,
    }, text


def trip_result(
    answer: ProviderAnswer, catalogue: Stations, now: datetime
) -> dict[str, Any] | Refused:
    if not timedelta() <= to_utc(now) - to_utc(answer.fetched_at) < FRESH_FOR:
        return Refused(
            "The NS trip data is stale or has an invalid check time. Try again.", RefusalKind.RETRY
        )
    routes = []
    for route in answer.connections:
        if not route.legs or not _usable(route):
            continue
        current = current_connection((answer,), route, merge_crowd_forecasts=True)
        if (current.origin, current.destination) != (answer.query.origin, answer.query.destination):
            continue
        departed = _departure(current.legs[0]) < to_utc(now)
        if (
            not departed
            and _usable(current)
            and all(
                (arrival := _arrival(leg)) is None or arrival >= _departure(leg)
                for leg in current.legs
            )
        ):
            routes.append(current)

    def soonest(route: Connection) -> tuple[datetime, datetime, int]:
        arrival = _arrival(route.legs[-1])
        return (
            _departure(route.legs[0]),
            arrival or datetime.max.replace(tzinfo=UTC),
            len(route.legs),
        )

    checked_at = answer.fetched_at.astimezone(TZ)
    origin, destination = (
        catalogue.name(code) for code in (answer.query.origin, answer.query.destination)
    )
    lines = [
        f"{origin} to {destination}",
        f"NS check {checked_at:%d %b at %H:%M} (Amsterdam time).",
    ]
    options: list[dict[str, Any]] = []
    seen = set()
    for route in sorted(routes, key=soonest):
        identity = tuple((leg.train, leg.board.code, leg.alight.code) for leg in route.legs)
        if identity in seen:
            continue
        seen.add(identity)
        legs, texts = zip(*(_leg(leg, catalogue, checked_at) for leg in route.legs), strict=True)
        options.append({"legs": list(legs)})
        lines.append(f"{len(options)}. " + "\n   ".join(texts))
        if len(options) == 3:
            break
    if not options:
        lines.append("NS returned no usable upcoming train options for this route.")
    return {
        "status": "ok" if options else "no_options",
        "checked_at": checked_at.isoformat(),
        "origin": {"code": answer.query.origin, "name": origin},
        "destination": {"code": answer.query.destination, "name": destination},
        "options": options,
        "text": "\n".join(lines),
    }

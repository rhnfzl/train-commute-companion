"""Describe recovery journeys without treating a bus or a walk as a watched train.

This module validates provider itineraries. It never selects a journey or infers physical
progress. Physical identifiers must describe boarding places, not station-code aliases.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Literal

from train_commute_companion.calendar import TZ, hhmm
from train_commute_companion.location import STALE_LOCATION
from train_commute_companion.model import Connection, Provenance, Stop, TimeObs
from train_commute_companion.serde import encode
from train_commute_companion.transfer import transfer_minimum

Mode = Literal["TRAIN", "BUS", "METRO", "TRAM", "FERRY", "WALK"]
_MODES = frozenset({"TRAIN", "BUS", "METRO", "TRAM", "FERRY", "WALK"})


class RecoveryError(ValueError):
    """An itinerary lacks enough consistent evidence to describe a physical journey."""


@dataclass(frozen=True, slots=True)
class RecoveryPlaceKey:
    physical_id: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    physical_id: str
    station_code: str
    latitude: float
    longitude: float

    @property
    def physical_key(self) -> RecoveryPlaceKey:
        return RecoveryPlaceKey(
            self.physical_id, float(self.latitude) + 0.0, float(self.longitude) + 0.0
        )


@dataclass(frozen=True, slots=True)
class RecoveryServiceKey:
    mode: Mode
    operator: str
    service_reference: str
    service_date: date
    planned_departure: datetime
    origin: RecoveryPlaceKey
    destination: RecoveryPlaceKey


@dataclass(frozen=True, slots=True)
class RecoveryStep:
    mode: Mode
    origin: Place
    destination: Place
    departure: datetime
    arrival: datetime
    departure_provenance: Provenance
    arrival_provenance: Provenance
    operator: str | None
    service_reference: str | None
    planned_departure: datetime | None = None
    planned_arrival: datetime | None = None

    @property
    def service_key(self) -> RecoveryServiceKey | None:
        if (
            self.mode == "WALK"
            or not self.operator
            or not self.operator.strip()
            or not self.service_reference
            or not self.service_reference.strip()
            or self.planned_departure is None
        ):
            return None
        try:
            departure = _utc(self.planned_departure)
        except RecoveryError:
            return None
        return RecoveryServiceKey(
            self.mode,
            self.operator,
            self.service_reference,
            self.planned_departure.astimezone(TZ).date(),
            departure,
            self.origin.physical_key,
            self.destination.physical_key,
        )

    @property
    def reported_times(self) -> bool:
        return self.departure_provenance is self.arrival_provenance is Provenance.REPORTED


@dataclass(frozen=True, slots=True)
class RecoveryTransfer:
    incoming_step: int
    outgoing_step: int
    walk_steps: tuple[int, ...]
    walking: timedelta
    waiting: timedelta
    minimum: timedelta


@dataclass(frozen=True, slots=True)
class RecoveryItinerary:
    steps: tuple[RecoveryStep, ...]
    transfers: tuple[RecoveryTransfer, ...]

    @property
    def vehicle_changes(self) -> int:
        return len(self.transfers)

    @property
    def reported_times(self) -> bool:
        return all(step.reported_times for step in self.steps)


@dataclass(frozen=True, slots=True)
class CatchabilityResult:
    state: Literal["feasible", "unconfirmed", "not_catchable", "unknown"]
    reason: str


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise RecoveryError("time has no timezone")
    return at.astimezone(UTC)


def _place(stop: Stop) -> Place:
    if not stop.name.strip() or not stop.physical_id or not stop.physical_id.strip():
        raise RecoveryError("missing physical endpoint identity")
    lat, lon = stop.latitude, stop.longitude
    if lat is None or lon is None:
        raise RecoveryError("endpoint coordinates are missing")
    if (
        isinstance(lat, bool)
        or isinstance(lon, bool)
        or not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        raise RecoveryError("endpoint coordinates are invalid")
    return Place(stop.name, stop.physical_id, stop.code, lat, lon)


def _same_place(a: Place, b: Place) -> bool:
    return a.physical_id == b.physical_id and (a.latitude, a.longitude) == (b.latitude, b.longitude)


def _time(obs: TimeObs | None, planned: datetime | None) -> tuple[datetime, Provenance]:
    if obs is not None and obs.provenance is Provenance.MISSING:
        raise RecoveryError("endpoint time is missing")
    if obs is not None and obs.provenance is Provenance.REPORTED:
        if obs.value is None:
            raise RecoveryError("reported endpoint time is missing")
        _utc(obs.value)
        return obs.value, Provenance.REPORTED
    if planned is None:
        raise RecoveryError("planned endpoint time is missing")
    _utc(planned)
    return planned, Provenance.PLANNED


def from_connection(
    connection: Connection, transfer_min: Mapping[str, int] | None = None
) -> RecoveryItinerary:
    """Require explicit mode and physical metadata. Never recover them from category or TrainId."""
    if transfer_min is not None and (
        not isinstance(transfer_min, Mapping)
        or any(
            not isinstance(key, str)
            or not key.strip()
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in transfer_min.items()
        )
    ):
        raise RecoveryError("transfer minimum must map station codes to nonnegative whole minutes")
    limits = transfer_min if transfer_min is not None else {}
    if connection.status not in ("NORMAL", "ALTERNATIVE_TRANSPORT"):
        raise RecoveryError("trip status cannot establish usable transport")
    if not connection.legs:
        raise RecoveryError("itinerary has no steps")
    steps: list[RecoveryStep] = []
    for leg in connection.legs:
        if leg.transport_mode not in _MODES:
            raise RecoveryError("transport mode is unknown")
        if len(leg.stops) < 2:
            raise RecoveryError("step has missing endpoints")
        if leg.cancelled or leg.part_cancelled or any(s.cancelled for s in leg.stops):
            raise RecoveryError("step is cancelled or cut short")
        if leg.reachable is False or leg.board.passing or leg.alight.passing:
            raise RecoveryError("step endpoints cannot be used")
        departure, dep_source = _time(leg.board.departure, leg.board.planned_departure)
        arrival, arr_source = _time(leg.alight.arrival, leg.alight.planned_arrival)
        if _utc(arrival) < _utc(departure):
            raise RecoveryError("step time is reversed")
        step = RecoveryStep(
            leg.transport_mode,
            _place(leg.board),
            _place(leg.alight),
            departure,
            arrival,
            dep_source,
            arr_source,
            leg.operator,
            leg.service_reference,
            leg.board.planned_departure,
            leg.alight.planned_arrival,
        )
        if steps:
            previous = steps[-1]
            if not _same_place(previous.destination, step.origin):
                raise RecoveryError(
                    "physical transfer is disconnected; an explicit walk is required"
                )
            if _utc(step.departure) < _utc(previous.arrival):
                raise RecoveryError("transfer time is reversed")
        steps.append(step)
    vehicles = [i for i, step in enumerate(steps) if step.mode != "WALK"]
    transfers = []
    for incoming, outgoing in pairwise(vehicles):
        if connection.legs[incoming].change_possible is False:
            raise RecoveryError("provider: vehicle change is not possible")
        walks = tuple(i for i in range(incoming + 1, outgoing) if steps[i].mode == "WALK")
        walking = sum(
            (_utc(steps[i].arrival) - _utc(steps[i].departure) for i in walks), timedelta()
        )
        available = _utc(steps[outgoing].departure) - _utc(steps[incoming].arrival)
        minimum = timedelta(
            minutes=transfer_minimum(
                connection.legs[outgoing].board.code,
                limits,
                connection.legs[incoming].transfer_time_to_next,
                connection.legs[outgoing].transfer_message_min,
            )
        )
        if available < minimum:
            raise RecoveryError("transfer time is below the required minimum")
        transfers.append(
            RecoveryTransfer(incoming, outgoing, walks, walking, available - walking, minimum)
        )
    return RecoveryItinerary(tuple(steps), tuple(transfers))


def validate_catchability(
    itinerary: RecoveryItinerary,
    confirmed_origin: Place | None,
    confirmed_at: datetime | None,
    now: datetime,
    *,
    max_origin_age: timedelta = STALE_LOCATION,
    checked_at: datetime | None = None,
) -> CatchabilityResult:
    """Check a fresh physical origin against this itinerary, without replanning or claiming progress."""
    from train_commute_companion.health import FRESH_FOR

    if not itinerary.steps or confirmed_origin is None or confirmed_at is None:
        return CatchabilityResult("unknown", "physical origin is not confirmed")
    if checked_at is None:
        return CatchabilityResult("unknown", "provider check time is missing")
    source_age = _utc(now) - _utc(checked_at)
    if source_age < timedelta() or source_age >= FRESH_FOR:
        return CatchabilityResult("unknown", "provider itinerary is not current")
    if max_origin_age < timedelta():
        raise RecoveryError("origin age limit must not be negative")
    age = _utc(now) - _utc(confirmed_at)
    if age < timedelta() or age > max_origin_age:
        return CatchabilityResult("unknown", "physical origin confirmation is not current")
    first = itinerary.steps[0]
    if not _same_place(confirmed_origin, first.origin):
        return CatchabilityResult("unknown", "confirmed origin is a different physical place")
    if _utc(now) > _utc(first.departure):
        return CatchabilityResult(
            "not_catchable", "the first scheduled departure is before the current time"
        )
    if not itinerary.reported_times:
        return CatchabilityResult("unconfirmed", "at least one endpoint time is planned only")
    return CatchabilityResult(
        "feasible", "physical origin and reported times permit this itinerary"
    )


def payload(itinerary: RecoveryItinerary) -> dict[str, object]:
    """Portable display fields with explicit duration units, without travel-progress claims."""
    return {
        "steps": encode(itinerary.steps),
        "transfers": [
            {
                "incoming_step": change.incoming_step,
                "outgoing_step": change.outgoing_step,
                "walk_steps": list(change.walk_steps),
                "walking_seconds": change.walking.total_seconds(),
                "waiting_seconds": change.waiting.total_seconds(),
                "minimum_seconds": change.minimum.total_seconds(),
            }
            for change in itinerary.transfers
        ],
        "vehicle_changes": itinerary.vehicle_changes,
        "reported_times": itinerary.reported_times,
        "followed": False,
        "origin_confirmed": False,
        "accessibility": "unknown",
    }


def render(itinerary: RecoveryItinerary) -> str:
    """Plain proposed directions. A timetable alone never confirms boarding or arrival."""
    header = "Proposed recovery itinerary (not followed)"
    if not itinerary.reported_times:
        header += " (unconfirmed: planned times)"
    lines = [header + ".", "Accessibility and assistance availability are not verified."]
    changes = {change.outgoing_step: change for change in itinerary.transfers}
    for index, step in enumerate(itinerary.steps):
        if index in changes:
            change = changes[index]
            lines.append(
                f"Change vehicle: {int(change.walking.total_seconds() // 60)} min walking, "
                f"{int(change.waiting.total_seconds() // 60)} min waiting."
            )
        service = " ".join(
            part for part in (step.operator, step.mode.title(), step.service_reference) if part
        )
        lines.append(
            f"{hhmm(step.departure)}-{hhmm(step.arrival)} {service}: "
            f"{step.origin.name} to {step.destination.name}."
        )
    return "\n".join(lines)

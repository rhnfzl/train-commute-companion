"""Read-only NS vehicle context. Equipment and capacity do not prove availability."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from train_commute_companion.model import TrainId
from train_commute_companion.serde import encode

FRESH_FOR = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class SeatCapacity:
    first_class: int | None = None
    second_class: int | None = None
    folding_first_class: int | None = None
    folding_second_class: int | None = None
    bicycle_spaces: int | None = None


@dataclass(frozen=True, slots=True)
class StockUnit:
    number: str | None
    type: str | None
    equipment: tuple[str, ...] | None
    capacity: SeatCapacity


@dataclass(frozen=True, slots=True)
class PlatformFeature:
    type: str
    description: str | None


@dataclass(frozen=True, slots=True)
class TrainContext:
    requested_train: TrainId
    station: str
    fetched_at: datetime
    source: str | None
    train_type: str | None
    operator: str | None
    platform: str | None
    shortened: bool | None
    carriage_count: int | None
    length_meters: float | None
    units: tuple[StockUnit, ...] | None
    platform_features: tuple[PlatformFeature, ...] | None
    layout_direction: Literal["left", "right"] | None
    # These responses carry neither measurement time nor corroborated operating date.
    measurement_at: datetime | None = None
    operating_date_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class VehiclePosition:
    train_number: str
    ride_id: str | None
    latitude: float
    longitude: float
    source: str | None
    train_type: str | None
    # Keep reported values separate. Their units were not established by an official schema.
    reported_speed: float | None
    reported_heading: float | None
    reported_accuracy: float | None
    measurement_at: datetime | None = None
    operating_date_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class VehiclePositions:
    fetched_at: datetime
    positions: tuple[VehiclePosition, ...]


@runtime_checkable
class VehicleProvider(Protocol):
    async def train_context(self, train: TrainId, station: str) -> TrainContext: ...

    async def vehicle_positions(self) -> VehiclePositions: ...


@dataclass(frozen=True, slots=True)
class VehicleWatch:
    train: TrainId
    station: str
    journey_checked_at: datetime


@dataclass(frozen=True, slots=True)
class VehicleSnapshot:
    watch: VehicleWatch
    context: TrainContext | None
    position: VehiclePosition | None
    context_checked_at: datetime | None
    positions_checked_at: datetime | None
    coverage: Literal["unknown"] = "unknown"
    measurement_at: datetime | None = None
    operating_date_confirmed: bool = False
    capacity_is_availability: bool = False
    equipment_operation_confirmed: bool = False


def is_fresh(value: TrainContext | VehiclePositions, now: datetime) -> bool:
    """Fresh retrieval does not establish when NS measured a vehicle position."""
    if any(t.tzinfo is None or t.utcoffset() is None for t in (now, value.fetched_at)):
        return False
    return timedelta() <= now.astimezone(UTC) - value.fetched_at.astimezone(UTC) < FRESH_FOR


def payload(value: TrainContext | VehiclePositions, now: datetime) -> dict[str, object]:
    """Portable fields with explicit unknown measurement time and no progress claims."""
    fields: dict[str, object] = encode(value)
    return {
        **fields,
        "fresh": is_fresh(value, now),
        "measurement_time_known": False,
        "capacity_is_availability": False,
        "equipment_operation_confirmed": False,
        "boarding_confirmed": False,
    }


_CAPACITY_LABELS = {
    "first_class": "first-class seats",
    "second_class": "second-class seats",
    "folding_first_class": "first-class folding seats",
    "folding_second_class": "second-class folding seats",
    "bicycle_spaces": "bicycle spaces",
}
_EQUIPMENT_LABELS = {
    "WIFI": "Wi-Fi",
    "TOILET": "toilet",
    "STILTE": "quiet area",
    "FIETS": "bicycle space",
    "STROOM": "power",
}
_PLATFORM_LABELS = {"TRAP": "stairs", "ROLTRAP": "escalator", "LIFT": "lift"}


def _capacity_totals(context: TrainContext | None) -> dict[str, int | None]:
    totals: dict[str, int | None] = dict.fromkeys(_CAPACITY_LABELS)
    if context is None or not context.units:
        return totals
    for field in totals:
        counts = [getattr(unit.capacity, field) for unit in context.units]
        if all(
            isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for count in counts
        ):
            totals[field] = sum(counts)
    return totals


def snapshot_payload(snapshot: VehicleSnapshot) -> dict[str, object]:
    """Serialize a monitor snapshot, whose stale context and position have already been hidden."""
    fields: dict[str, object] = encode(snapshot)
    return {
        **fields,
        "capacity_totals": _capacity_totals(snapshot.context),
        "position_is_candidate": snapshot.position is not None,
        "measurement_time_known": False,
        "boarding_confirmed": False,
        "arrival_confirmed": False,
    }


def render_snapshot(snapshot: VehicleSnapshot) -> str:
    """Describe retrieved evidence without claiming measurement freshness or travel progress."""
    watch = snapshot.watch
    lines = [
        (
            f"NS train context: service {watch.train.train_number} at {watch.station}, "
            f"requested date {watch.train.operating_date.isoformat()} (service date unverified)."
        )
    ]
    context = snapshot.context
    if context is None:
        lines.append("Train and platform context is unavailable or not current.")
        if snapshot.context_checked_at is not None:
            lines.append(
                "Last context retrieval: "
                + snapshot.context_checked_at.isoformat(timespec="seconds")
                + "."
            )
    else:
        lines.append(
            f"Context retrieved: {context.fetched_at.isoformat(timespec='seconds')}; "
            f"source {context.source or 'unknown'}."
        )
        stock = (
            " ".join(part for part in (context.operator, context.train_type) if part) or "unknown"
        )
        carriages = "unknown" if context.carriage_count is None else str(context.carriage_count)
        length = "unknown" if context.length_meters is None else f"{context.length_meters:g} m"
        shortened = (
            "unknown" if context.shortened is None else ("yes" if context.shortened else "no")
        )
        lines.append(
            f"Listed train: {stock}; carriages {carriages}; length {length}; shortened {shortened}."
        )
        totals = _capacity_totals(context)
        capacity = "; ".join(
            f"{label}: {count if count is not None else 'unknown'}"
            for field, label in _CAPACITY_LABELS.items()
            for count in (totals[field],)
        )
        lines.append("Listed capacity, not free-seat availability: " + capacity + ".")
        equipment = sorted(
            {
                _EQUIPMENT_LABELS.get(item, item)
                for unit in context.units or ()
                for item in unit.equipment or ()
            }
        )
        lines.append(
            "Listed equipment: " + (", ".join(equipment) or "unknown") + ". Operation is unknown."
        )
        features = sorted(
            {
                "section " + feature.description
                if feature.type == "PERRONLETTER" and feature.description
                else _PLATFORM_LABELS.get(feature.type, feature.type)
                for feature in context.platform_features or ()
            }
        )
        lines.append(
            f"Platform {context.platform or 'unknown'}; listed platform features: "
            + (", ".join(features) or "unknown")
            + ". Operation is unknown."
        )
    position = snapshot.position
    if position is None:
        lines.append("Vehicle position is unavailable or not current.")
        if snapshot.positions_checked_at is not None:
            lines.append(
                "Last position retrieval: "
                + snapshot.positions_checked_at.isoformat(timespec="seconds")
                + "."
            )
    else:
        checked = (
            snapshot.positions_checked_at.isoformat(timespec="seconds")
            if snapshot.positions_checked_at is not None
            else "unknown"
        )
        lines.append(
            f"Position candidate: {position.latitude:.6f}, {position.longitude:.6f}; "
            f"source {position.source or 'unknown'}; retrieved {checked}."
        )
    lines.append("Measurement time is unknown. Vehicle data does not confirm boarding or arrival.")
    return "\n".join(lines)

"""Per-leg classification and train identity (6.1, 6.2). Pure functions, no clock."""

from datetime import UTC, datetime

from train_commute_companion.model import (
    ClassifiedLeg,
    Leg,
    LegKind,
    Observation,
    Stop,
    TimeObs,
    TrainId,
)
from train_commute_companion.providers.base import ProviderAnswer

DELAYED_AT_MIN = 3


def _delay(obs: TimeObs | None) -> int | None:
    if obs is None or obs.planned is None or obs.value is None:
        return None
    return max(0, int((obs.value - obs.planned).total_seconds() // 60))


def classify_leg(leg: Leg, alight_station: str) -> ClassifiedLeg:
    """Kind comes from the stops between boarding and `alight_station`, never from one flag."""
    stops = leg.stops
    # shortcut: a station the train never calls at falls back to its last stop
    end = next(
        (i for i, s in enumerate(stops) if i > 0 and s.code == alight_station), len(stops) - 1
    )
    chain = stops[: end + 1]
    kind = LegKind.OK
    last_served: Stop | None = None
    if chain[0].cancelled:
        kind = LegKind.CANCELLED
    elif chain[-1].cancelled:
        served = [s for s in chain[1:] if not s.cancelled and not s.passing]
        if served:
            kind, last_served = LegKind.CUT_SHORT, served[-1]
        else:
            kind = LegKind.CANCELLED
    dep = _delay(chain[0].departure)
    arr = _delay(chain[-1].arrival)
    if kind is LegKind.CANCELLED:
        dep, arr = 0, None
    elif kind is LegKind.CUT_SHORT:
        arr = None
    elif (dep or 0) >= DELAYED_AT_MIN or (arr or 0) >= DELAYED_AT_MIN:
        kind = LegKind.DELAYED
    return ClassifiedLeg(leg, kind, Observation.SEEN, dep, arr, last_served)


def _minute(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(second=0, microsecond=0)


def find_leg(
    answer: ProviderAnswer, train: TrainId, board_station: str
) -> tuple[Leg | None, Observation]:
    """Exact planned departure at `board_station` plus the stored number; never by proximity."""
    want = _minute(train.planned_departure)
    found: dict[tuple[object, str], Leg] = {}
    for conn in answer.connections:
        for leg in conn.legs:
            dep = leg.board.planned_departure
            if leg.board.code == board_station and dep is not None and _minute(dep) == want:
                found.setdefault((leg.train.operating_date, leg.train.train_number), leg)
    if not found:
        return None, Observation.MISSING
    if len(found) == 1:
        ((key, leg),) = found.items()
        if key == (train.operating_date, train.train_number):
            return leg, Observation.SEEN
    return None, Observation.UNCERTAIN

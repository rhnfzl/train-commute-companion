"""Per-leg classification and train identity (6.1, 6.2). Pure functions, no clock."""

from collections.abc import Iterable
from datetime import UTC, datetime

from train_commute_companion.model import (
    ClassifiedLeg,
    Connection,
    Leg,
    LegKind,
    Observation,
    Provenance,
    Stop,
    TimeObs,
    TrainId,
)
from train_commute_companion.providers.base import ProviderAnswer

DELAYED_AT_MIN = 3


def _delay(obs: TimeObs | None) -> int | None:
    if obs is None or obs.planned is None or obs.value is None:
        return None
    # same-zone subtraction ignores the DST fold, so subtract instants
    return max(
        0, int((obs.value.astimezone(UTC) - obs.planned.astimezone(UTC)).total_seconds() // 60)
    )


def served(s: Stop) -> bool:
    """The train calls here: not passing, and not cancelled unless it has a reported time
    (rule 14: a cancelled stop with a reported, actual, time was still served)."""
    reported = any(o and o.provenance is Provenance.REPORTED for o in (s.arrival, s.departure))
    return not s.passing and (not s.cancelled or reported)


def _expected_departure(conn: Connection) -> datetime:
    d = conn.legs[0].board.departure
    t = d.value if d and d.value else d.planned if d else None
    return (t or conn.legs[0].train.planned_departure).astimezone(UTC)


def sort_by_expected_departure(conns: Iterable[Connection]) -> list[Connection]:
    return sorted(conns, key=_expected_departure)


def classify_leg(leg: Leg, alight_station: str) -> ClassifiedLeg:
    """Kind comes from the stops between boarding and `alight_station`, never from one flag.
    A train that does not call at `alight_station` (absent, passed or cancelled there) is cut
    short at its last served stop, or cancelled when it serves none; never ok."""
    stops = leg.stops
    end = next((i for i, s in enumerate(stops) if i > 0 and s.code == alight_station), None)
    chain = stops if end is None else stops[: end + 1]
    kind = LegKind.OK
    last_served: Stop | None = None
    if not served(chain[0]):
        kind = LegKind.CANCELLED
    elif end is None or not served(chain[-1]):
        calls = [s for s in chain[1:] if served(s) and s.code != alight_station]
        if calls:
            kind, last_served = LegKind.CUT_SHORT, calls[-1]
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

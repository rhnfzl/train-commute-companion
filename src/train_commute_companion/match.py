"""Usual-journey matching and route-change detection (6.3, 7.12 rule 3). Pure functions."""

from collections.abc import Sequence
from datetime import UTC, datetime

from train_commute_companion.calendar import TZ
from train_commute_companion.model import Connection, Leg, Signature, TrainId
from train_commute_companion.providers.base import ProviderAnswer


def matches_pattern(c: Connection, transfers: tuple[str, ...]) -> bool:
    return c.is_train_only and c.transfer_stations == transfers


def _minute(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(second=0, microsecond=0)


def _arrival(leg: Leg) -> datetime:
    o = leg.alight.arrival
    return o.value if o and o.value else leg.alight.planned_arrival or leg.train.planned_departure


def _dep(leg: Leg) -> datetime:
    return leg.board.planned_departure or leg.train.planned_departure


def _fits(c: Connection, sig: Signature, first_departure: datetime) -> bool:
    if len(c.legs) != len(sig.legs):
        return False
    base = first_departure.astimezone(TZ).date()  # clock time and day_offset are Amsterdam-local
    for leg, s in zip(c.legs, sig.legs, strict=True):
        dep = _dep(leg).astimezone(TZ)
        arr = (leg.alight.planned_arrival or dep).astimezone(TZ)
        if (
            (leg.board.code, leg.alight.code) != (s.board, s.alight)
            or dep.timetz().replace(tzinfo=None) != s.departure
            or (dep.date() - base).days != s.day_offset
            or int((arr - dep).total_seconds() // 60) != s.duration_min
        ):
            return False
    return True


def usual_connection(
    answer: ProviderAnswer,
    origin: str,
    first_departure: datetime,
    transfers: tuple[str, ...],
    signature: Signature | None,
) -> Connection | None:
    """The matching connection whose leg 1 leaves `origin` at `first_departure` (6.3)."""
    want = _minute(first_departure)
    found = [
        c
        for c in answer.connections
        if matches_pattern(c, transfers)
        and c.legs[0].board.code == origin
        and _minute(_dep(c.legs[0])) == want
    ]
    if signature is not None:
        found = [c for c in found if _fits(c, signature, first_departure)] or found
    if not found:
        return None
    # later legs' departures are the 6.3 tie-break after the earliest arrival
    return min(found, key=lambda c: (_arrival(c.legs[-1]), *(_dep(x) for x in c.legs[1:])))


def remaining_pattern(full_transfers: tuple[str, ...], from_station: str) -> tuple[str, ...]:
    """Transfers after `from_station`; a station that is not a transfer leaves the full pattern."""
    if from_station in full_transfers:
        return full_transfers[full_transfers.index(from_station) + 1 :]
    return full_transfers


def route_changed(
    locked_suffix: Sequence[TrainId], answer: ProviderAnswer, expected_transfers: tuple[str, ...]
) -> Connection | None:
    """A connection that starts on the first locked train but uses another transfer pattern.
    None when the usual pattern is still offered with that train, or the train is not offered."""
    if not locked_suffix:
        return None
    first = locked_suffix[0]
    starts = [c for c in answer.connections if c.is_train_only and c.legs[0].train == first]
    if not starts or any(c.transfer_stations == expected_transfers for c in starts):
        return None
    return min(starts, key=lambda c: _arrival(c.legs[-1]))

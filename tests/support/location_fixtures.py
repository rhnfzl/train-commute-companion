"""Shared location fixtures: a station report on the usual two-leg journey."""

from lifecycle_fixtures import L1, L2, ans, at, conn, ctx

from train_commute_companion.lifecycle import Transition, on_location
from train_commute_companion.location import (
    LocationEvent,
)
from train_commute_companion.state import OccurrenceState

USUAL = conn(L1, L2)


def event(zone: str, kind: str, when: str) -> LocationEvent:
    return LocationEvent(zone, kind, at(when))  # type: ignore[arg-type]


def locate(s: OccurrenceState, now: str, e: LocationEvent, *conns: object) -> Transition:
    a = ans("AAA", *conns, when=now) if conns else None  # type: ignore[arg-type]
    return on_location(s, ctx(at(now)), e, a)

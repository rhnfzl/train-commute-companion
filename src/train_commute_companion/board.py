"""The one `is_good` predicate and the replacement chooser (7.12 rule 1, 7.4, 6.3). Pure."""

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypedDict, Unpack

from train_commute_companion.classify import classify_leg, sort_by_expected_departure
from train_commute_companion.match import matches_pattern
from train_commute_companion.model import (
    ClassifiedLeg,
    Connection,
    LegKind,
    Observation,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.transfer import check_transfer


@dataclass(frozen=True, slots=True)
class Catchability:
    not_before: datetime


def catch_from(
    now: datetime, buffer_min: int, tap_at: datetime | None, at_station: bool
) -> Catchability:
    buffer = timedelta(minutes=buffer_min)
    if at_station:
        return Catchability(now)
    if tap_at is not None:
        return Catchability(max(now, tap_at + buffer))
    return Catchability(now + buffer)


@dataclass(frozen=True, slots=True)
class GoodCheck:
    ok: bool
    reason: str


def max_transfers(usual: tuple[str, ...]) -> int:
    return len(usual) + 1


class GoodKwargs(TypedDict):
    board_station: str
    destination: str
    catch: Catchability
    kept_incoming: ClassifiedLeg | None
    done_transfers: int
    max_transfers: int
    profile_min: Mapping[str, int]


def is_good(c: Connection, **kw: Unpack[GoodKwargs]) -> GoodCheck:
    """`done_transfers` counts transfers made before `kept_incoming` boarded; the transfer from
    `kept_incoming` into the first new leg is counted here."""
    board, kept, pm = kw["board_station"], kw["kept_incoming"], kw["profile_min"]

    def bad(reason: str) -> GoodCheck:
        return GoodCheck(False, reason)

    if not c.is_train_only:
        return bad("not train only")
    if c.origin != board:
        return bad("does not board at the boarding station")
    if c.destination != kw["destination"]:
        return bad("does not arrive at the destination")
    total = kw["done_transfers"] + (kept is not None) + len(c.legs) - 1
    if total > kw["max_transfers"]:
        return bad("over the transfer cap")
    cls = [classify_leg(leg, leg.alight.code) for leg in c.legs]
    for x in cls:
        if x.kind in (LegKind.CANCELLED, LegKind.CUT_SHORT):
            return bad(f"leg {x.kind.value}")
        if x.observation is not Observation.SEEN:
            return bad(f"leg {x.observation.value}")
    first = c.legs[0].board.departure
    dep = (first.value or first.planned) if first else None
    dep = dep or c.legs[0].train.planned_departure
    if dep < kw["catch"].not_before:
        return bad("first leg cannot be caught")
    pairs = [(kept, cls[0], board)] if kept is not None else []
    pairs += [(a, b, a.leg.alight.code) for a, b in itertools.pairwise(cls)]
    for inc, out, station in pairs:
        tc = check_transfer(inc, out, station, pm)
        if tc.state is not TransferState.OK:
            return bad(f"transfer at {station} {tc.state.value}: {tc.reason}")
    return GoodCheck(True, "ok")


def choose_replacement(
    answer: ProviderAnswer, *, usual_remaining: tuple[str, ...], **kw: Unpack[GoodKwargs]
) -> tuple[Connection | None, Literal["usual_pattern", "new_route", "none"]]:
    good = [c for c in sort_by_expected_departure(answer.connections) if is_good(c, **kw).ok]
    for c in good:
        if matches_pattern(c, usual_remaining):
            return c, "usual_pattern"
    return (good[0], "new_route") if good else (None, "none")

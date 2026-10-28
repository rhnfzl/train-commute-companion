"""The one `is_good` predicate and the replacement chooser (7.12 rule 1, 7.4, 6.3). Pure."""

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict, Unpack

from train_commute_companion.classify import (
    _expected_departure,
    classify_leg,
    current_connection,
    evidence,
    find_leg,
    pair_transfer,
)
from train_commute_companion.match import matches_pattern
from train_commute_companion.model import (
    ClassifiedLeg,
    Connection,
    LegKind,
    Observation,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.transfer import check_transfer, solid


@dataclass(frozen=True, slots=True)
class Catchability:
    not_before: datetime  # UTC: wall-clock addition across the DST change is wrong


def catch_from(
    now: datetime, buffer_min: int, tap_at: datetime | None, at_station: bool
) -> Catchability:
    buffer = timedelta(minutes=buffer_min)
    now = now.astimezone(UTC)
    tap_at = None if tap_at is None else tap_at.astimezone(UTC)
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


def is_good(c: Connection, answer: ProviderAnswer, **kw: Unpack[GoodKwargs]) -> GoodCheck:
    """`done_transfers` counts transfers made before `kept_incoming` boarded; the transfer from
    `kept_incoming` into the first new leg is counted here. Every leg is identified in `answer`
    (6.1): a leg that shares its boarding minute with another train is uncertain."""
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
    # judge the current copy of every train (6.2) against `c`'s own alight stops
    seen_in = evidence(answer)
    cur = current_connection(seen_in, c)
    cls = [
        replace(
            classify_leg(cur.legs[i], leg.alight.code),
            observation=find_leg(answer, leg.train, leg.board.code)[1],
        )
        for i, leg in enumerate(c.legs)
    ]
    for x in cls:
        if x.kind in (LegKind.CANCELLED, LegKind.CUT_SHORT):
            return bad(f"leg {x.kind.value}")
        if x.observation is not Observation.SEEN:
            return bad(f"leg {x.observation.value}")
        # rule 20: a planned or missing time at either end of a leg is at risk, never good
        if not (solid(x.leg.board.departure) and solid(x.leg.alight.arrival)):
            return bad("leg has a planned or missing time")
    first = cls[0].leg.board.departure
    assert first is not None and first.value is not None  # solid() above
    if first.value.astimezone(UTC) < kw["catch"].not_before.astimezone(UTC):
        return bad("first leg cannot be caught")
    pairs = [(*pair_transfer(seen_in, kept, cls[0]), board)] if kept is not None else []
    pairs += [(a, b, leg.alight.code) for (a, b), leg in zip(itertools.pairwise(cls), c.legs)]
    for inc, out, station in pairs:
        tc = check_transfer(inc, out, station, pm)
        if tc.state is not TransferState.OK:
            return bad(f"transfer at {station} {tc.state.value}: {tc.reason}")
    return GoodCheck(True, "ok")


def choose_replacement(
    answer: ProviderAnswer,
    *,
    usual_remaining: tuple[str, ...],
    candidates: Sequence[Connection] | None = None,
    **kw: Unpack[GoodKwargs],
) -> tuple[Connection | None, Literal["usual_pattern", "new_route", "none"]]:
    """Ranks `candidates` (default: every connection in `answer`); identity is always judged
    against the whole `answer`, so dropping a train from the ranking never hides that it shares
    a minute with a candidate (6.1)."""
    pool = answer.connections if candidates is None else candidates
    # ranked and returned as current copies, judged on the raw ones (their alight stops)
    seen_in = evidence(answer)
    cur = {id(c): current_connection(seen_in, c) for c in pool}
    ranked = sorted(pool, key=lambda c: _expected_departure(cur[id(c)]))
    good = [c for c in ranked if is_good(c, answer, **kw).ok]
    for c in good:
        if matches_pattern(c, usual_remaining):
            return cur[id(c)], "usual_pattern"
    return (cur[id(good[0])], "new_route") if good else (None, "none")

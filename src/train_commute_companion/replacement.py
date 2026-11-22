"""The one `check_good` rule and the replacement chooser (7.12 rule 1, 7.4, 6.3). Pure."""

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict, Unpack

from train_commute_companion.arrival import ArrivalChoice
from train_commute_companion.catalogue import Phrase, phrase
from train_commute_companion.classify import (
    classify_leg,
    current_connection,
    evidence,
    expected_departure,
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
from train_commute_companion.transfer import check_transfer, is_reported


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
    detail: Phrase | None = None

    @property
    def reason(self) -> str:
        return "ok" if self.detail is None else self.detail.render("en")


def max_transfers(usual: tuple[str, ...]) -> int:
    return len(usual) + 1


class GoodKwargs(TypedDict):
    board_station: str
    destination: str
    catch: Catchability
    kept_incoming: ClassifiedLeg | None
    done_transfers: int
    max_transfers: int | None
    profile_min: Mapping[str, int]


def _leg_problem(x: ClassifiedLeg, *, allow_planned: bool) -> Phrase | None:
    if x.kind in (LegKind.CANCELLED, LegKind.CUT_SHORT):
        return phrase(f"choice.{x.kind.value}")
    if x.observation is not Observation.SEEN:
        return phrase(f"choice.{x.observation.value}")
    # Only a timetable preview may use planned times in place of reported times.
    times = (x.leg.board.departure, x.leg.alight.arrival)
    if not all(is_reported(t) or allow_planned and t is not None and t.planned for t in times):
        return phrase("choice.missing_time")
    return None


def check_good(
    c: Connection,
    answer: ProviderAnswer,
    *,
    allow_planned: bool = False,
    known_first: bool = False,
    **kw: Unpack[GoodKwargs],
) -> GoodCheck:
    """`done_transfers` counts transfers made before `kept_incoming` boarded; the transfer from
    `kept_incoming` into the first new leg is counted here. Every leg is identified in `answer`
    (6.1): a leg that shares its boarding minute with another train is uncertain."""
    board, kept, pm = kw["board_station"], kw["kept_incoming"], kw["profile_min"]

    if not c.is_train_only:
        return GoodCheck(False, phrase("choice.not_train"))
    if c.origin != board:
        return GoodCheck(False, phrase("choice.boarding"))
    if c.destination != kw["destination"]:
        return GoodCheck(False, phrase("choice.destination"))
    total = kw["done_transfers"] + (kept is not None) + len(c.legs) - 1
    cap = kw["max_transfers"]
    if cap is not None and total > cap:
        return GoodCheck(False, phrase("choice.transfer_cap"))
    # judge the current copy of every train (6.2) against `c`'s own alight stops
    seen_in = evidence(answer)
    cur = current_connection(seen_in, c)
    if not cur.is_train_only:  # the copy returned is the one that must be a train (6.3)
        return GoodCheck(False, phrase("choice.not_train"))
    cls = [
        replace(
            classify_leg(cur.legs[i], leg.alight.code),
            observation=Observation.SEEN
            if known_first and i == 0
            else find_leg(answer, leg.train, leg.board.code)[1],
        )
        for i, leg in enumerate(c.legs)
    ]
    for x in cls:
        if (problem := _leg_problem(x, allow_planned=allow_planned)) is not None:
            return GoodCheck(False, problem)
    first = expected_departure(cur)
    if first.astimezone(UTC) < kw["catch"].not_before.astimezone(UTC):
        return GoodCheck(False, phrase("choice.too_late"))
    pairs = [(*pair_transfer(seen_in, kept, cls[0]), board)] if kept is not None else []
    pairs += [(a, b, leg.alight.code) for (a, b), leg in zip(itertools.pairwise(cls), c.legs)]
    for inc, out, station in pairs:
        tc = check_transfer(inc, out, station, pm, allow_planned=allow_planned)
        if tc.state is not TransferState.OK:
            return GoodCheck(
                False,
                phrase(
                    "choice.transfer",
                    station=station,
                    state=phrase(f"transfer.{tc.state.value}"),
                    reason=phrase(f"transfer.reason.{tc.reason}"),
                ),
            )
    return GoodCheck(True)


def choose_replacement(
    answer: ProviderAnswer,
    *,
    usual_remaining: tuple[str, ...] | None,
    candidates: Sequence[Connection] | None = None,
    arrival: ArrivalChoice | None = None,
    earliest: bool = False,
    allow_planned: bool = False,
    known_first: bool = False,
    **kw: Unpack[GoodKwargs],
) -> tuple[Connection | None, Literal["usual_pattern", "new_route", "none"]]:
    """Ranks `candidates` (default: every connection in `answer`); identity is always judged
    against the whole `answer`, so dropping a train from the ranking never hides that it shares
    a minute with a candidate (6.1). `usual_remaining` None: the earliest good one, no route
    preferred (window mode, an other boarding station)."""
    pool = answer.connections if candidates is None else candidates
    # ranked and returned as current copies, judged on the raw ones (their alight stops)
    seen_in = evidence(answer)
    cur = {id(c): current_connection(seen_in, c) for c in pool}
    ranked = sorted(pool, key=lambda c: expected_departure(cur[id(c)]))
    good = [
        c
        for c in ranked
        if check_good(c, answer, allow_planned=allow_planned, known_first=known_first, **kw).ok
    ]
    if arrival is not None:
        eligible = [
            cur[id(c)] for c in good if arrival.accepts(cur[id(c)], allow_planned=allow_planned)
        ]
        choice = (
            next(iter(eligible), None)
            if earliest
            else arrival.pick(eligible, allow_planned=allow_planned)
        )
        return choice, "new_route" if choice is not None else "none"
    for c in good:
        if usual_remaining is not None and matches_pattern(c, usual_remaining):
            return cur[id(c)], "usual_pattern"
    return (cur[id(good[0])], "new_route") if good else (None, "none")

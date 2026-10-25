"""Earlier-train advice over the whole journey (7.6, 7.12 rule 17). Pure, no clock."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from train_commute_companion.board import choose_replacement, is_good, max_transfers
from train_commute_companion.classify import classify_leg, find_leg
from train_commute_companion.lifecycle import (
    Ctx,
    DirectionState,
    LegState,
    OccurrenceState,
    _event,
    catch_from_state,
)
from train_commute_companion.match import usual_connection
from train_commute_companion.model import (
    AdviceRecord,
    AlertEvent,
    Connection,
    EventKind,
    LegKind,
    Observation,
    Severity,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.transfer import check_transfer


def _arrival(c: Connection) -> datetime | None:
    a = c.legs[-1].alight
    t = a.arrival.value if a.arrival and a.arrival.value else a.planned_arrival
    return None if t is None else t.astimezone(UTC)


def _departure(c: Connection) -> datetime:
    return c.legs[0].train.planned_departure.astimezone(UTC)


def _unrideable(c: Connection, answer: ProviderAnswer, profile_min: dict[str, int]) -> bool:
    """A leg is cancelled or cut short, or a transfer is broken: waiting does not get you there."""
    cls = [
        replace(
            classify_leg(x, x.alight.code),
            observation=find_leg(answer, x.train, x.board.code)[1],
        )
        for x in c.legs
    ]
    if any(x.kind in (LegKind.CANCELLED, LegKind.CUT_SHORT) for x in cls):
        return True
    return any(
        check_transfer(a, b, a.leg.alight.code, profile_min).state is TransferState.BROKEN
        for a, b in itertools.pairwise(cls)
    )


def _ridden(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Connection | None:
    """The connection the user would ride by waiting: the locked one, else the usual one.
    A lock not listed as one connection is rebuilt from its legs as observed (6.1), so a
    lock spread over connections still counts; None when a leg is not seen."""
    if s.lock is not None:
        first = s.lock.legs[0].train
        trains = [x.train for x in s.lock.legs]
        listed = next(
            (
                c
                for c in answer.connections
                if c.legs[0].train == first and [x.train for x in c.legs] == trains
            ),
            None,
        )
        if listed is not None:
            return listed
        found = [find_leg(answer, x.train, x.board_station) for x in s.lock.legs]
        if any(leg is None or ob is not Observation.SEEN for leg, ob in found):
            return None
        return Connection(tuple(leg for leg, _ in found if leg is not None), "", None)
    j = ctx.profile.journey(ctx.direction)
    origin = ctx.profile.origin(ctx.direction)
    return usual_connection(
        answer, origin, ctx.schedule.first_departure, j.transfers, ctx.signature
    )


def _gate(s: OccurrenceState, ctx: Ctx) -> bool:
    """Rule 17: idle or locked-but-not-travelling; no location or presence needed. Travel is
    judged by evidence (a tap or a departed leg), never by BOARDING, which only means the
    locked train is within the buffer: the escape train's catchability ends advice itself."""
    if not ctx.profile.journey(ctx.direction).earlier_train_advice or s.not_travelling:
        return False
    if s.direction_state is DirectionState.IDLE:
        return True
    return (
        s.direction_state is DirectionState.LOCKED
        and not s.travelling
        and s.lock is not None
        and all(x.state in (LegState.PENDING, LegState.BOARDING) for x in s.lock.legs)
    )


_FAR = datetime(2100, 1, 1, tzinfo=UTC)


def advise(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, usual_planned_arrival: datetime
) -> tuple[AdviceRecord, list[AlertEvent]]:
    """`answer` is a healthy board from the occurrence's origin. The earlier first train must
    pass `is_good` (which also enforces catching it with the buffer, so advice ends by itself
    at the escape train's departure minus buffer)."""
    record = s.advice or AdviceRecord(None, None, False)
    if not _gate(s, ctx):
        return record, []
    prof, d = ctx.profile, ctx.direction
    usual = prof.journey(d)
    ride = _ridden(s, ctx, answer)
    # what waiting means: the lock's first train, else the effective usual first departure
    wait_from = (
        s.lock.legs[0].train.planned_departure if s.lock else ctx.schedule.first_departure
    ).astimezone(UTC)
    catch = catch_from_state(s, usual.buffer_min, ctx.now)
    kw = {
        "board_station": prof.origin(d),
        "destination": prof.destination(d),
        "catch": catch,
        "kept_incoming": None,
        "done_transfers": 0,
        "max_transfers": max_transfers(usual.transfers),
        "profile_min": prof.transfer_min,
    }
    # not seen in a healthy answer: the journey the user waits for is not there, so waiting
    # means the first later good train, like after a cancellation (7.6, rule 2)
    ride_arr = None if ride is None else _arrival(ride)
    broken = ride is None or _unrideable(ride, answer, dict(prof.transfer_min))
    slow = ride_arr is not None and ride_arr >= usual_planned_arrival.astimezone(UTC) + timedelta(
        minutes=prof.alerts.earlier_train_threshold_min
    )
    bad = broken or slow
    if ride is not None:
        wait_from = _departure(ride)
    if broken:
        # board 59: what you ride if you wait is the first later good train, not the cancelled one
        later = [c for c in answer.connections if c is not ride and _departure(c) >= wait_from]
        rep, _ = choose_replacement(
            answer,
            usual_remaining=usual.transfers,
            candidates=later,
            **kw,  # type: ignore[arg-type]
        )
        ride_arr = None if rep is None else _arrival(rep)
    best: tuple[timedelta, Connection] | None = None
    if bad:
        need = timedelta(minutes=prof.alerts.earlier_train_min_saving_min)
        for c in answer.connections:
            arr = _arrival(c)
            if arr is None or _departure(c) >= wait_from:
                continue
            saving = (ride_arr or _FAR) - arr  # no baseline: soonest arrival wins
            if saving < need or not is_good(c, answer, **kw).ok:  # type: ignore[arg-type]
                continue
            # largest saving; a tie goes to the earlier train
            if (
                best is None
                or saving > best[0]
                or (saving == best[0] and _departure(c) < _departure(best[1]))
            ):
                best = (saving, c)
    advised = record.advised_train
    if best is not None:
        train = best[1].legs[0].train
        if advised == train and not record.withdrawn:
            return record, []  # already told
        # renewed after a withdrawal: a new episode with its own delivery identity (7.6)
        first = best[1].legs[0]
        dep_t = first.board.departure.value if first.board.departure else None
        shown = (dep_t or first.train.planned_departure).astimezone(ctx.now.tzinfo)
        text = (
            f"leave now, an earlier train {first.board.code} to {best[1].legs[-1].alight.code}"
            f" leaves at {shown:%H:%M} and gets you there sooner than your usual journey"
        )
        ev = _event(ctx, EventKind.LEAVE_NOW, Severity.LOUD, text, train, ctx.now.isoformat())
        return AdviceRecord(train, ctx.now, False), [ev]
    if advised is None or record.withdrawn or bad:
        return record, []
    # withdraw once: usual journey is back, the advised train still runs and is catchable
    leg, seen = find_leg(answer, advised, prof.origin(d))
    if leg is None or seen is not Observation.SEEN:
        return record, []
    kind = classify_leg(leg, leg.alight.code).kind
    dep = leg.board.departure
    t = dep.value if dep and dep.value else None
    if kind in (LegKind.CANCELLED, LegKind.CUT_SHORT) or t is None:
        return record, []
    if t.astimezone(UTC) < catch.not_before.astimezone(UTC):
        return record, []
    ev = _event(
        ctx,
        EventKind.EARLIER_TRAIN_WITHDRAWN,
        Severity.SILENT,
        "earlier train no longer needed",
        advised,
        "" if record.sent_at is None else record.sent_at.isoformat(),  # the episode it ends
    )
    return replace(record, withdrawn=True), [ev]

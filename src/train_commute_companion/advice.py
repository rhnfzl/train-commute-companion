"""Earlier-train advice over the whole journey (7.6, 7.12 rule 17). Pure, no clock."""

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
        for a, b in zip(cls, cls[1:], strict=False)
    )


def _ridden(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Connection | None:
    """The connection the user would ride by waiting: the locked one, else the usual one."""
    if s.lock is not None:
        first = s.lock.legs[0].train
        trains = [x.train for x in s.lock.legs]
        return next(
            (
                c
                for c in answer.connections
                if c.legs[0].train == first and [x.train for x in c.legs] == trains
            ),
            None,
        )
    j = ctx.profile.journey(ctx.direction)
    origin = ctx.profile.origin(ctx.direction)
    return usual_connection(
        answer, origin, ctx.schedule.first_departure, j.transfers, ctx.signature
    )


def _gate(s: OccurrenceState, ctx: Ctx) -> bool:
    """Rule 17: idle or locked-but-not-travelling; no location or presence needed."""
    if not ctx.profile.journey(ctx.direction).earlier_train_advice or s.not_travelling:
        return False
    if s.direction_state is DirectionState.IDLE:
        return True
    return (
        s.direction_state is DirectionState.LOCKED
        and not s.travelling
        and s.lock is not None
        and all(x.state is LegState.PENDING for x in s.lock.legs)
    )


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
    if ride is None:
        return record, []  # not listed: nothing to compare against, stay silent
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
    ride_arr = _arrival(ride)
    broken = _unrideable(ride, answer, dict(prof.transfer_min))
    slow = ride_arr is not None and ride_arr >= usual_planned_arrival.astimezone(UTC) + timedelta(
        minutes=prof.alerts.earlier_train_threshold_min
    )
    bad = broken or slow
    if broken:
        # board 59: what you ride if you wait is the first later good train, not the cancelled one
        later = [
            c for c in answer.connections if c is not ride and _departure(c) >= _departure(ride)
        ]
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
            if arr is None or _departure(c) >= _departure(ride):
                continue
            saving = timedelta.max if ride_arr is None else ride_arr - arr
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
        if advised == train:
            return record, []  # already told (also after a withdrawal: withdraw once)
        text = (
            f"leave now, an earlier train leaves at {best[1].legs[0].train.planned_departure:%H:%M}"
            f" and gets you there sooner than your usual journey"
        )
        ev = _event(ctx, EventKind.LEAVE_NOW, Severity.LOUD, text, train)
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
    )
    return replace(record, withdrawn=True), [ev]

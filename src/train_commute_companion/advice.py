"""Earlier-train advice over the whole journey (7.6, 7.12 rule 17). Pure, no clock."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo

from train_commute_companion.board import choose_replacement, is_good, max_transfers
from train_commute_companion.classify import (
    classify_leg,
    current_connection,
    evidence,
    find_leg,
)
from train_commute_companion.lifecycle import (
    Ctx,
    DirectionState,
    LegProgress,
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
    Leg,
    LegKind,
    Observation,
    Severity,
    TrainId,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.transfer import check_transfer


def _arrival(c: Connection) -> datetime | None:
    a = c.legs[-1].alight
    t = a.arrival.value if a.arrival and a.arrival.value else a.planned_arrival
    return None if t is None else t.astimezone(UTC)


def _departure(c: Connection) -> datetime:
    return c.legs[0].train.planned_departure.astimezone(UTC)


def _trains(c: Connection) -> tuple[TrainId, ...]:
    return tuple(x.train for x in c.legs)


def _unrideable(c: Connection, answer: ProviderAnswer, profile_min: dict[str, int]) -> bool:
    """A leg is cancelled or cut short, or a transfer is broken: waiting does not get you there.
    Each leg is judged as the newest evidence shows it, against `c`'s own alighting stop (6.2)."""
    cur = current_connection(evidence(answer), c)
    cls = [
        replace(
            classify_leg(cur.legs[i], x.alight.code),
            observation=find_leg(answer, x.train, x.board.code)[1],
        )
        for i, x in enumerate(c.legs)
    ]
    nxt = [*cls[1:], None]
    for x, n in zip(cls, nxt, strict=True):
        if x.kind is LegKind.CANCELLED:
            return True
        # a cut-short train with a listed onward leg from its last served stop is a way there
        if x.kind is LegKind.CUT_SHORT and not (
            n is not None and x.last_served is not None and n.leg.board.code == x.last_served.code
        ):
            return True
    return any(
        check_transfer(a, b, b.leg.board.code, profile_min).state is TransferState.BROKEN
        for a, b in itertools.pairwise(cls)
    )


def _segment(answer: ProviderAnswer, x: LegProgress) -> Leg | None:
    """The locked train over its stored stops, from a copy in the answer that reaches the
    stored alighting stop. A copy cut where its own connection changes earlier says nothing
    about the rest, so with no copy reaching that stop the segment is unavailable."""
    leg, ob = find_leg(answer, x.train, x.board_station)
    if leg is None or ob is not Observation.SEEN:
        return None
    for g in (g for c in answer.connections for g in c.legs):
        if g.train != leg.train or g.board.code != x.board_station:
            continue
        end = next((i for i, st in enumerate(g.stops) if i and st.code == x.alight_stop), None)
        if end is not None:
            return replace(g, stops=g.stops[: end + 1])
    return None


def _ridden(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Connection | None:
    """The connection the user would ride by waiting: the locked one, else the usual one.
    The lock is rebuilt leg by leg against its stored boarding and alighting stops (6.1), so
    a lock spread over connections still counts and a fragment ending early is not its
    arrival; None when a leg's whole segment is not seen."""
    if s.lock is not None:
        found = [_segment(answer, x) for x in s.lock.legs]
        legs = tuple(g for g in found if g is not None)
        return Connection(legs, "", None) if len(legs) == len(found) else None
    j = ctx.profile.journey(ctx.direction)
    origin = ctx.profile.origin(ctx.direction)
    usual = usual_connection(
        answer, origin, ctx.schedule.first_departure, j.transfers, ctx.signature
    )
    if usual is not None:
        return usual
    # the usual train cut short with a listed change onward (6.3 N-leg model: its pattern differs)
    want = ctx.schedule.first_departure.astimezone(UTC)
    cut = [
        c
        for c in answer.connections
        if c.legs[0].board.code == origin
        and c.legs[0].train.planned_departure.astimezone(UTC) == want
        and c.legs[-1].alight.code == ctx.profile.destination(ctx.direction)
        and len(c.legs) == len(j.transfers) + 2
    ]
    return min(cut, key=lambda c: _arrival(c) or _FAR) if cut else None


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


def _kw(s: OccurrenceState, ctx: Ctx) -> dict[str, object]:
    prof, d = ctx.profile, ctx.direction
    usual = prof.journey(d)
    return {
        "board_station": prof.origin(d),
        "destination": prof.destination(d),
        "catch": catch_from_state(s, usual.buffer_min, ctx.now),
        "kept_incoming": None,
        "done_transfers": 0,
        "max_transfers": max_transfers(usual.transfers),
        "profile_min": prof.transfer_min,
    }


def _judge(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, reference: datetime | None
) -> tuple[Connection | None, bool, bool]:
    """(the journey ridden by waiting as its current copy, broken, bad) (7.6). Lateness needs
    the usual's planned arrival; a journey that is not there or not rideable is bad without one."""
    prof = ctx.profile
    ride = _ridden(s, ctx, answer)
    # not seen in a healthy answer: the journey the user waits for is not there, so waiting
    # means the first later good train, like after a cancellation (7.6, rule 2)
    broken = ride is None or _unrideable(ride, answer, dict(prof.transfer_min))
    cur = None if ride is None else current_connection(evidence(answer), ride)
    # a current copy ending before the destination has no arrival there (broken says so)
    ride_arr = (
        _arrival(cur)
        if cur is not None
        and ride is not None
        and cur.legs[-1].alight.code == ride.legs[-1].alight.code
        else None
    )
    limit = timedelta(minutes=prof.alerts.earlier_train_threshold_min)
    slow = (
        ride_arr is not None
        and reference is not None
        and ride_arr >= reference.astimezone(UTC) + limit
    )
    return cur, broken, broken or slow


def needed(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, reference: datetime | None
) -> bool:
    """Whether waiting is bad now (7.6): what a withdrawal says no longer holds once it is."""
    return _judge(s, ctx, answer, reference)[2]


def advice_query(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, reference: datetime | None
) -> Query | None:
    """The origin searched again from the earliest catchable time, so a train before the one
    `answer` was asked from is in view (7.6, rule 17); only while advice is gated and waiting is
    bad or advice is out (a withdrawal needs to see the advised train)."""
    if not _gate(s, ctx):
        return None
    a = s.advice
    out = a is not None and a.advised_train is not None and not a.withdrawn
    if not out and not needed(s, ctx, answer, reference):
        return None
    at = catch_from_state(s, ctx.profile.journey(ctx.direction).buffer_min, ctx.now).not_before
    if at >= answer.query.at.astimezone(UTC):
        return None
    d = ctx.direction
    return Query(ctx.profile.origin(d), ctx.profile.destination(d), at.astimezone(ctx.now.tzinfo))


def still_advised(
    s: OccurrenceState,
    ctx: Ctx,
    answer: ProviderAnswer,
    train: TrainId,
    reference: datetime | None,
) -> Connection | None:
    """Rule 5(b): the advised train's connection as `answer` shows it now, while advice is
    still gated, waiting is still bad and the train still saves enough and passes `is_good`
    with the current catch (so its current departure minus buffer is its expiry)."""
    if not _gate(s, ctx):
        return None
    found = _candidates(s, ctx, answer, reference)
    return next((c for _, c in found if c.legs[0].train == train), None)


def _candidates(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, reference: datetime | None
) -> list[tuple[timedelta, Connection]]:
    """(saving, connection) for every earlier first train that beats waiting by the minimum
    saving and passes `is_good` with the current catch; empty while waiting is not bad (7.6)."""
    prof, d = ctx.profile, ctx.direction
    usual = prof.journey(d)
    # what waiting means: the lock's first train, else the effective usual first departure
    wait_from = (
        s.lock.legs[0].train.planned_departure if s.lock else ctx.schedule.first_departure
    ).astimezone(UTC)
    kw = _kw(s, ctx)
    ride, broken, bad = _judge(s, ctx, answer, reference)
    ride_arr = None if ride is None else _arrival(ride)
    if ride is not None:
        wait_from = _departure(ride)
    if broken:
        # board 59: what you ride if you wait is the first later good train, not the cancelled one
        mine = None if ride is None else _trains(ride)
        later = [c for c in answer.connections if _trains(c) != mine and _departure(c) >= wait_from]
        rep, _ = choose_replacement(
            answer,
            usual_remaining=usual.transfers,
            candidates=later,
            **kw,  # type: ignore[arg-type]
        )
        ride_arr = None if rep is None else _arrival(rep)
    found: list[tuple[timedelta, Connection]] = []
    if bad:
        need = timedelta(minutes=prof.alerts.earlier_train_min_saving_min)
        seen_in, done = evidence(answer), set()
        for c in answer.connections:  # newest answer first: its copy of a journey is kept
            if (trains := _trains(c)) in done:
                continue
            done.add(trains)
            cur = current_connection(seen_in, c)  # saving and text use current times
            arr = _arrival(cur)
            if arr is None or _departure(cur) >= wait_from:
                continue
            saving = (ride_arr or _FAR) - arr  # no baseline: soonest arrival wins
            if saving < need or not is_good(c, answer, **kw).ok:  # type: ignore[arg-type]
                continue
            found.append((saving, cur))
    return found


def leave_now_text(c: Connection, tz: tzinfo | None) -> str:
    first = c.legs[0]
    dep_t = first.board.departure.value if first.board.departure else None
    shown = (dep_t or first.train.planned_departure).astimezone(tz)
    return (
        f"leave now, an earlier train {first.board.code} to {c.legs[-1].alight.code}"
        f" leaves at {shown:%H:%M} and gets you there sooner than your usual journey"
    )


def advise(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, usual_planned_arrival: datetime | None
) -> tuple[AdviceRecord, list[AlertEvent]]:
    """`answer` is a healthy board from the occurrence's origin. The earlier first train must
    pass `is_good` (which also enforces catching it with the buffer, so advice ends by itself
    at the escape train's departure minus buffer). Without the usual's planned arrival only
    lateness is not judged: an absent or unrideable journey is still bad."""
    record = s.advice or AdviceRecord(None, None, False, delivered=False)
    if not _gate(s, ctx):
        return record, []
    prof, d = ctx.profile, ctx.direction
    catch = catch_from_state(s, prof.journey(d).buffer_min, ctx.now)
    cands = _candidates(s, ctx, answer, usual_planned_arrival)
    # largest saving; a tie goes to the earlier train
    best = min(cands, key=lambda x: (-x[0], _departure(x[1])), default=None)
    bad = needed(s, ctx, answer, usual_planned_arrival)
    advised = record.advised_train
    if best is not None:
        train = best[1].legs[0].train
        if advised == train and not record.withdrawn:
            return record, []  # already told
        # renewed after a withdrawal: a new episode with its own delivery identity (7.6)
        text = leave_now_text(best[1], ctx.now.tzinfo)
        ev = _event(ctx, EventKind.LEAVE_NOW, Severity.LOUD, text, train, ctx.now.isoformat())
        return AdviceRecord(train, ctx.now, False, delivered=False), [ev]
    if advised is None or record.withdrawn or bad:
        return record, []
    if not record.delivered:  # never told: nothing to withdraw, its LEAVE_NOW is not sent
        return replace(record, withdrawn=True), []
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

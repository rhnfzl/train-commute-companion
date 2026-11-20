"""Time-driven steps and termination (7.8, 7.12 rules 6, 7, 11). Pure: `now` always comes from the
caller."""

from dataclasses import replace
from datetime import datetime

from train_commute_companion.calendar import hhmm, to_utc
from train_commute_companion.lifecycle import (
    Transition,
    auto_lock,
    bumped,
    first_open,
    make_event,
    on_prefix,
    place_zone,
    unproven_station,
)
from train_commute_companion.lifecycle_lock import may_be_on
from train_commute_companion.model import (
    FOLLOW_LIMIT,
    AlertEvent,
    Ctx,
    DirectionState,
    EventKind,
    LegState,
    Profile,
    Severity,
    present,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.state import (
    ArchivePhase,
    Commitment,
    Journey,
    LegProgress,
    OccurrenceState,
)


def on_tick(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer | None = None) -> Transition:
    """Time-driven steps: the errand timeout (45 min) clears a provisional lock, keeping a
    departed train retained (an other station's departed train stays the lock); the auto lock when due (`answer` is the usual's query answer); a
    window closed with no lock; each journey's end by its last watch or its 150-min limit
    (rules 6, 7). Arrival acceptance itself runs on each poll. Only a poll passes the usual's
    answer; any other tick has none and writes no auto lock."""
    if s.not_travelling:
        return Transition(s, ())
    t = _tick(s, ctx, answer)
    return Transition(bumped(s, t.state), t.events)  # termination and removal: rule 21


def _tick(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer | None) -> Transition:
    now = to_utc(ctx.now)
    events: list[AlertEvent] = []
    lock = s.lock
    if s.provisional_expired(now) and _left_other_station(s):
        # a known other station's train already left: it stays the lock and ends by the
        # journey-end rules (quiet when never seen boarding), not mid-ride by the errand timeout
        s = replace(s, errand_deadline=None)
    elif s.provisional_expired(now):
        lock = present(lock)
        f = first_open(lock)
        retained = s.retained
        if f < len(lock.legs) and may_be_on(lock.legs[f], now):
            unproven = lock.unproven or unproven_station(s)
            retained += (replace(lock, legs=lock.legs[f:], unproven=unproven),)
        state = s.direction_state
        # a known other station followed by itself and let go: the usual origin again
        back = lock.legs[0].board_station == s.boarding_station
        s = replace(
            s,
            lock=None,
            boarding_station=None if back else s.boarding_station,
            retained=retained,
            travelling=False,
            tap_at=None,
            errand_deadline=None,
            generation=s.generation + 1,
            direction_state=DirectionState.IDLE if state is DirectionState.LOCKED else state,
        )
    if answer is not None and s.lock is None and s.direction_state is not DirectionState.DONE:
        t = auto_lock(s, ctx, answer)
        s, events = t.state, [*events, *t.events]
        if s.lock is not None:
            s = _merge_retained(s)
    if (
        s.lock is None
        and s.direction_state is not DirectionState.DONE
        and now >= to_utc(ctx.schedule.window_end)
    ):
        # a train the user never boarded is no lost journey; on_leave with no leave is no trip
        if not s.released and ctx.schedule.pick != "on_leave":
            text = "I stopped following your journey, check the NS app."
            events.append(make_event(ctx, EventKind.STOPPED_FOLLOWING, Severity.LOUD, text))
        s = replace(s, direction_state=DirectionState.DONE)
    if s.lock is not None and s.direction_state is not DirectionState.DONE:
        end = _journey_end(s, ctx, s.lock, retained=False)
        if end and _never_boarded(s, ctx, s.lock, retained=False):
            s = replace(
                s,
                lock=None,
                released=True,
                travelling=False,
                tap_at=None,
                errand_deadline=None,
                direction_state=DirectionState.IDLE,
            )
        elif end is not None:
            events += end
            s = replace(s, direction_state=DirectionState.DONE)
    left = []
    for r in s.retained:
        end = _journey_end(s, ctx, r, retained=True)
        if end is None:
            left.append(r)
        elif not (end and _never_boarded(s, ctx, r, retained=True)):
            events += end
    return Transition(replace(s, retained=tuple(left)), tuple(events))


def _left_other_station(s: OccurrenceState) -> bool:
    """The lock boards at the other station and a poll saw its first train depart (RIDING is set
    by the expected departure). shortcut: a train NS never identified stays PENDING and is
    retained by its planned time as before; read the pool here if that case shows up."""
    first = present(s.lock).legs[0]
    return first.board_station == s.boarding_station and first.state in (
        LegState.RIDING,
        LegState.DONE,
    )


def _merge_retained(s: OccurrenceState) -> OccurrenceState:
    """The auto lock may follow the very journey a cleared lock kept: a retained remainder
    whose unfinished trains are exactly the lock's from its first one on is merged into the
    lock with its progress and watches. Any other remainder, also one that shares only its
    first train, stays retained (rule 7)."""
    lock = present(s.lock)
    legs = list(lock.legs)
    trains = [x.train for x in legs]
    kept = []
    for r in s.retained:
        rest = r.legs[first_open(r) :]
        i = trains.index(rest[0].train) if rest and rest[0].train in trains else None
        if i is None or trains[i:] != [x.train for x in rest]:
            kept.append(r)
            continue
        legs[i:] = rest
    return replace(s, lock=replace(lock, legs=tuple(legs)), retained=tuple(kept))


def _arrived(s: OccurrenceState, ctx: Ctx, last: LegProgress) -> bool:
    """A location report time-stamped after the accepted arrival, at the alighting station or
    the destination zone (rule 6)."""
    when = last.accept_value or last.accepted_at
    if when is None:
        return False
    p, d = ctx.profile, ctx.direction
    zones = {last.alight_stop, p.destination(d), place_zone(p, p.destination(d))}
    return any((t := s.zone_reports.get(z)) is not None and to_utc(t) > to_utc(when) for z in zones)


def arrival_evidence(s: OccurrenceState, p: Profile) -> tuple[datetime | None, datetime | None]:
    """(first phone presence at the destination, accepted train arrival), each None while
    unknown. A report from before the locked train left is not an arrival."""
    d, lock = s.key.direction, s.lock
    last = lock.legs[-1] if lock is not None else None
    train = None
    if last is not None and last.state is LegState.DONE:
        train = last.accept_value or last.accepted_at
    zones = {p.destination(d), place_zone(p, p.destination(d))}
    if last is not None:
        zones.add(last.alight_stop)
    after = None if lock is None else to_utc(lock.legs[0].train.planned_departure)
    seen = [
        t for z, t in s.zone_reports.items() if z in zones and (after is None or to_utc(t) >= after)
    ]
    return (min(seen, key=to_utc) if seen else None), train


def _never_boarded(s: OccurrenceState, ctx: Ctx, j: Journey, *, retained: bool) -> bool:
    """2026-11-18: with location on, the user was never at the station `j` starts from nor at
    any station it changes at. A train that only NS saw leave and arrive was a guess (Q12 rule
    5), so its end says nothing about the user. A station fix commits the lock it was taken for
    (`_at_first_station`, also with a lagging phone), and a retained remainder keeps that, so a
    later visit for another train (`at_station`, the origin's zone report) never counts for it."""
    if ctx.profile.location is None or j.commitment is Commitment.COMMITTED:
        return False
    if not retained and s.at_station:
        return False
    return not any(x.board_station in s.zone_reports for x in j.legs[1:])


def _journey_end(
    s: OccurrenceState, ctx: Ctx, j: Journey, *, retained: bool
) -> tuple[AlertEvent, ...] | None:
    """None while `j` is followed; else the events of its end: silent with arrival evidence,
    else one loud `stopped_following` naming it."""
    now = to_utc(ctx.now)
    last = j.legs[-1]
    first = j.legs[0].train
    on = on_prefix(j) if retained else ""
    train = first if retained else None
    limit = to_utc(j.planned_final_arrival) + FOLLOW_LIMIT
    if last.state is LegState.DONE:
        # the earlier of the last watch and the journey's 150-min limit (rules 6, 7)
        w = last.watch_until
        if now < limit and (w is None or now < to_utc(w)):
            return None
        if _arrived(s, ctx, last):
            return ()
        arrival = hhmm(last.accept_value or last.accepted_at or w or limit)
        text = (
            f"{on}NS showed it arriving at {arrival}; I stopped following it, check the NS app."
            if retained
            else f"NS showed your train arriving at {arrival}; I stopped following, check the NS "
            "app if you are not there."
        )
        return (make_event(ctx, EventKind.STOPPED_FOLLOWING, Severity.LOUD, text, train),)
    if now < limit:
        return None
    text = f"{on}I stopped following {'it' if retained else 'your journey'}, check the NS app."
    return (make_event(ctx, EventKind.STOPPED_FOLLOWING, Severity.LOUD, text, train),)


def may_archive(s: OccurrenceState, now: datetime, pending_journey_warnings: int) -> bool:
    """Rule 6: the occurrence may be archived. The caller counts the occurrence's undelivered
    journey warnings in its ledger; none may remain."""
    return s.archive_phase(now, pending_journey_warnings) is ArchivePhase.READY

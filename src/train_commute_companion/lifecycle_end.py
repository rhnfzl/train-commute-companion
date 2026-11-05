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
)
from train_commute_companion.lifecycle_lock import may_be_on
from train_commute_companion.model import (
    FOLLOW_LIMIT,
    AlertEvent,
    Ctx,
    DirectionState,
    EventKind,
    LegState,
    Severity,
    present,
)
from train_commute_companion.providers.base import ProviderAnswer
from train_commute_companion.state import (
    Commitment,
    Journey,
    LegProgress,
    OccurrenceState,
)


def on_tick(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer | None = None) -> Transition:
    """Time-driven steps: the errand timeout (45 min) clears a provisional lock, keeping a
    departed train retained; the auto lock when due (`answer` is the usual's query answer); a
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
    if (
        lock is not None
        and lock.commitment is Commitment.PROVISIONAL
        and s.errand_deadline is not None
        and now >= to_utc(s.errand_deadline)
    ):
        f = first_open(lock)
        retained = s.retained
        if f < len(lock.legs) and may_be_on(lock.legs[f], now):
            retained += (replace(lock, legs=lock.legs[f:]),)
        state = s.direction_state
        s = replace(
            s,
            lock=None,
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
        text = "I stopped following your journey, check the NS app."
        events.append(make_event(ctx, EventKind.STOPPED_FOLLOWING, Severity.LOUD, text))
        s = replace(s, direction_state=DirectionState.DONE)
    if s.lock is not None and s.direction_state is not DirectionState.DONE:
        end = _journey_end(s, ctx, s.lock, retained=False)
        if end is not None:
            events += end
            s = replace(s, direction_state=DirectionState.DONE)
    left = []
    for r in s.retained:
        end = _journey_end(s, ctx, r, retained=True)
        if end is None:
            left.append(r)
        else:
            events += end
    return Transition(replace(s, retained=tuple(left)), tuple(events))


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
    if pending_journey_warnings > 0:
        return False
    if s.not_travelling:
        return True
    if s.direction_state is not DirectionState.DONE or s.retained:
        return False
    if s.lock is None:
        return True
    w = s.lock.legs[-1].watch_until
    limit = to_utc(s.lock.planned_final_arrival) + FOLLOW_LIMIT
    return w is None or to_utc(now) >= min(to_utc(w), limit)

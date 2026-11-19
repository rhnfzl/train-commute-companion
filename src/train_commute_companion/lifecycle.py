"""User actions, the auto lock, the follow step, location and arrival acceptance (7.2, 7.4, 7.7,
7.12 rules 1, 2, 3, 6, 7, 8, 10, 11, 21, 7.13). The lock writer is in `lifecycle_lock`, termination
in `lifecycle_end`. Pure: `now` always comes from the caller."""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import NamedTuple

from train_commute_companion.calendar import TZ, hhmm, resolve_local, to_utc
from train_commute_companion.classify import (
    classify_leg,
    current_connection,
    current_run,
    evidence,
    find_leg,
    pair_transfer,
    served,
)
from train_commute_companion.lifecycle_lock import (
    MAY_HAVE_LEFT,
    journey_of,
    may_be_on,
    progress_of,
    rides,
    unfinished_leg,
    write_lock,
)
from train_commute_companion.location import STALE_LOCATION, Evidence, LocationEvent
from train_commute_companion.match import (
    matches_pattern,
    remaining_pattern,
    route_changed,
    usual_connection,
)
from train_commute_companion.messages import (
    ON_TIME_MAX_MIN,
    crowd_text,
    data_unclear_text,
    status_text,
)
from train_commute_companion.model import (
    AlertEvent,
    AlertFacts,
    ClassifiedLeg,
    Connection,
    Ctx,
    Direction,
    DirectionState,
    EventKind,
    Leaving,
    Leg,
    LegKind,
    LegState,
    LiveDelay,
    NotTravelling,
    Observation,
    OccurrenceKey,
    Profile,
    Provenance,
    Severity,
    ToldDelay,
    TrainId,
    TransferState,
    alert_facts,
    display_name,
    okey,
    present,
    train_ident,
)
from train_commute_companion.providers.base import Pool, ProviderAnswer, Query
from train_commute_companion.replacement import (
    Catchability,
    GoodCheck,
    GoodKwargs,
    catch_from,
    check_good,
    choose_replacement,
    max_transfers,
)
from train_commute_companion.schedule import active_lead
from train_commute_companion.state import (
    ACCEPT_AFTER,
    ACCEPT_APART,
    ArrivalPhase,
    Commitment,
    EndpointPhase,
    Journey,
    LegProgress,
    LockSource,
    OccurrencePhase,
    OccurrenceState,
    PollRecord,
    PromptStage,
    StationPrompt,
    unclear_slot,
)
from train_commute_companion.transfer import check_transfer, transfer_minimum
from train_commute_companion.unclear import disagreement, newest_copies, train_key


@dataclass(frozen=True, slots=True)
class Transition:
    state: OccurrenceState
    events: tuple[AlertEvent, ...]


@dataclass(frozen=True, slots=True)
class Choose:
    service_date: date | None
    connection: Connection
    # the fresh healthy answers the connection was picked from (the latest poll): identity
    # (6.1) and the change from a kept leg (rule 1) are judged on them, never on the copy alone
    answers: tuple[ProviderAnswer, ...]


@dataclass(frozen=True, slots=True)
class ConfirmStation:
    """Yes or Other train: the user boards at `station`, not where the direction starts."""

    service_date: date | None
    station: str


@dataclass(frozen=True, slots=True)
class SetPrompt:
    """Replace or clear the open question (No, a new stage, an answer handled)."""

    service_date: date | None
    prompt: StationPrompt | None


UserAction = Choose | Leaving | NotTravelling | ConfirmStation | SetPrompt

_STATION_REPLACES = (LockSource.AUTO, LockSource.LOCATION)
_STATUS_SLOT = "at_station#"  # Q1: the station message of this occurrence was told
_DEPARTED = (LegState.RIDING, LegState.DONE)  # by departure evidence: boarding progress (7.4)


def is_resolved(s: OccurrenceState) -> bool:
    """Nothing left to follow (rule 6, consistent with `may_archive`): an occurrence whose main journey
    ended still runs while a retained remainder remains (rule 7)."""
    return s.phase is not OccurrencePhase.FOLLOWING


def boarding_origin(s: OccurrenceState, ctx: Ctx) -> str:
    """Where this occurrence boards: a confirmed other station, else the profile origin."""
    return s.boarding_station or ctx.profile.origin(ctx.direction)


def _plan(s: OccurrenceState) -> tuple[object, ...]:
    """What the generation guards (rule 21): every lock, leg progress, watch, location and
    action fact. Observed times (onward search time, acceptance candidates) are not plan
    changes, so polls in flight stay valid while they move."""

    def leg(x: LegProgress) -> LegProgress:
        return replace(x, onward_from=None, accept_value=None, accept_first_seen=None)

    def journey(j: Journey | None) -> Journey | None:
        return None if j is None else replace(j, legs=tuple(map(leg, j.legs)))

    return (
        journey(s.lock),
        tuple(map(journey, s.retained)),
        s.direction_state,
        s.travelling,
        s.tap_at,
        s.at_station,
        s.errand_deadline,
        s.not_travelling,
        s.cancellation_history,
        s.boarding_station,
    )


def bumped(old: OccurrenceState, new: OccurrenceState) -> OccurrenceState:
    """`new` with the generation raised once when its plan moved and nothing raised it yet."""
    if new.generation != old.generation or _plan(new) == _plan(old):
        return new
    return replace(new, generation=old.generation + 1)


def catch_from_state(s: OccurrenceState, buffer_min: int, now: datetime) -> Catchability:
    return catch_from(now, buffer_min, s.tap_at if s.travelling else None, s.at_station)


DELAY_STEP_MIN = 3


def next_stated_delay(last_min: int | None, reported_min: int | None) -> int | None:
    """The delay the user was last told, moved only by news (7.5): a 3-min step either way
    from what was said, or back to normal at 1 min or less. A wobble around the last stated
    delay keeps it, so +5 to +6 or +3 to +2 says nothing. No reported time keeps it too."""
    if reported_min is None:
        return last_min
    said = last_min or 0
    if said > ON_TIME_MAX_MIN and reported_min <= ON_TIME_MAX_MIN:
        return 0
    return reported_min if abs(reported_min - said) >= DELAY_STEP_MIN else last_min


def _recovery(
    e: AlertEvent, facts: AlertFacts, before: Sequence[int], said: Sequence[int]
) -> AlertEvent | None:
    """A stated delay falling to on time is "back to normal" (1 min or less), one falling
    under the 3-min step is "nearly on time" (7.5); an on-time train never stated late says
    nothing. A loud delay (past the arrival limit) stays a loud delay: the limit still binds."""
    if e.severity is Severity.LOUD:
        return e
    if max(said) <= ON_TIME_MAX_MIN:
        if max(before) <= ON_TIME_MAX_MIN:
            return None
        kind, tail = EventKind.BACK_TO_NORMAL, "is back to normal, on time per NS."
    elif max(said) < DELAY_STEP_MIN and max(before) > max(said):
        kind, tail = EventKind.NEARLY_ON_TIME, "is nearly on time."
    else:
        return e
    text = f"{display_name(e.profile_name)}: {facts.on}{facts.subject} train {tail}"
    key = kind.value + e.key[len(e.kind.value) :]
    return replace(e, kind=kind, severity=Severity.SILENT, text=text, key=key)


def _delay_slots(e: AlertEvent, f: AlertFacts) -> list[str]:
    return [e.key + (f"#{d.tag}" if d.tag else "") for d in f.delays]


def _stated_delays(e: AlertEvent, f: AlertFacts, keys: dict[str, str]) -> AlertEvent | None:
    """The event with its delays stated, remembered in `keys`; None when it is no news."""
    slots = _delay_slots(e, f)
    was = [None if keys.get(x) is None else int(keys[x]) for x in slots]
    live = [d.minutes for d in f.delays]
    told = [next_stated_delay(w, n) for w, n in zip(was, live, strict=True)]
    if told != was:  # news: the message states every live value, so each is remembered
        told = [t if n is None else n for t, n in zip(told, live, strict=True)]
    said = [t or 0 for t in told]
    before = [w or 0 for w in was]
    keys.update({x: str(t) for x, t in zip(slots, said, strict=True)})
    e = replace(
        e,
        key=f"{e.key}|stated+{','.join(map(str, said))}",
        facts=replace(
            f,
            delays=(),
            told=tuple(ToldDelay(x, str(t)) for x, t in zip(slots, said, strict=True)),
        ),
    )
    return _recovery(e, f, before, said) if e.kind is EventKind.DELAYED else e


def keep_stated(s: OccurrenceState, events: Sequence[AlertEvent]) -> Transition:
    """A stated replacement time or delay in a key moves only by news (7.5): the delay last
    stated per fact is kept in `alert_keys`, so 09:05 to 09:08 is news again while 09:05 to
    09:06 is not. A delay brought back to on time becomes its recovery message."""
    keys = dict(s.alert_keys)
    out = []
    for e in events:
        f = alert_facts(e)
        if f.crowd_slot and f.crowd_forecast is not None:
            previous = keys.get(f.crowd_slot)
            baseline = f"baseline:{f.crowd_forecast}"
            if previous is None and f.crowd_forecast != "high" or previous == baseline:
                keys[f.crowd_slot] = baseline
                continue
            keys[f.crowd_slot] = f.crowd_forecast
            out.append(e)
            continue
        if f.moved_slot:
            # per boarding segment: a train back on its planned platform is news only once a
            # change was announced for it (1 -> 5 -> 1 says so, an unchanged 1 says nothing)
            if not f.back_on_plan:
                keys[f.moved_slot] = "1"
            elif f.moved_slot not in keys:
                continue
            out.append(e)
            continue
        if not f.delays:
            out.append(e)
            continue
        news = _stated_delays(e, f, keys)
        if news is not None:
            out.append(news)
    return Transition(replace(s, alert_keys=keys) if keys != s.alert_keys else s, tuple(out))


def active_at(s: OccurrenceState, ctx: Ctx) -> datetime:
    """Q9: `active_lead` minutes before the earlier of the usual first departure and the locked
    journey's first planned departure. The usual bound keeps a later relock from reopening the
    quiet phase; the lock bound ends it early for an earlier chosen or relocked train."""
    first = to_utc(ctx.schedule.first_departure)
    if s.lock is not None:
        first = min(first, to_utc(s.lock.legs[0].train.planned_departure))
    return first - timedelta(minutes=active_lead(ctx.profile.journey(ctx.direction)))


def quiet_watch(s: OccurrenceState, ctx: Ctx) -> bool:
    """Q9 quiet early watch: time only, so location-driven messages are never held by it."""
    return to_utc(ctx.now) < active_at(s, ctx)


QUIET_DELAY_MIN = 5
# Q9: before the active point only the journey breaking or moving is told (7.9 severities kept)
QUIET_PASS = frozenset({
    EventKind.CANCELLED, EventKind.TRANSFER_BROKEN, EventKind.CUT_SHORT,
    EventKind.ROUTE_CHANGED, EventKind.PLATFORM_CHANGE, EventKind.NO_CONNECTION,
    EventKind.RESTORED, EventKind.STOPPED_FOLLOWING, EventKind.MISSED_FIRST_TRAIN,
    EventKind.MISSED_TRANSFER, EventKind.NEXT_CONNECTION, EventKind.AT_STATION,
    EventKind.STATION_ASK, EventKind.STATION_REMEMBER,
})  # fmt: skip


def quiet_events(
    s: OccurrenceState, ctx: Ctx, events: Sequence[AlertEvent]
) -> tuple[AlertEvent, ...]:
    """Q9 quiet early watch. Runs before `keep_stated`, so a delay held back here is never
    remembered as stated: at the active point it is news like any first delay (7.5). A delay
    passes from QUIET_DELAY_MIN live minutes, when loud (past the arrival limit), or when a delay
    of that train was already stated (> ON_TIME_MAX_MIN in its slot), so a change or a recovery
    of something the user heard is told."""
    if not quiet_watch(s, ctx):
        return tuple(events)

    def unusual(e: AlertEvent) -> bool:
        if e.kind in QUIET_PASS:
            return True
        if e.kind is not EventKind.DELAYED:
            return False
        f = alert_facts(e)
        told = (s.alert_keys.get(x) for x in _delay_slots(e, f))
        return (
            e.severity is Severity.LOUD
            or any((d.minutes or 0) >= QUIET_DELAY_MIN for d in f.delays)
            or any(x is not None and int(x) > ON_TIME_MAX_MIN for x in told)
        )

    return tuple(e for e in events if unusual(e))


def on_prefix(j: Journey) -> str:
    """A retained journey's warnings open with this, so it says which train it is about."""
    return f"if you are on the {hhmm(j.legs[0].train.planned_departure)}: "


def _event_key(ctx: Ctx, kind: EventKind, train: TrainId | None, key_facts: str) -> str:
    ident = "" if train is None else f":{train_ident(train)}"
    return f"{kind.value}:{ctx.schedule.key.service_date.isoformat()}{ident}:{key_facts}"


def make_event(
    ctx: Ctx,
    kind: EventKind,
    severity: Severity,
    text: str,
    train: TrainId | None = None,
    key_facts: str | None = None,
    stated: AlertFacts | None = None,
) -> AlertEvent:
    """The key carries the facts the message states (7.5): `key_facts` when given, else the text,
    so a changed endpoint, replacement or revised arrival is news while a repeat is not.
    Pass `key_facts` when the text holds a live time that drifts without being news. `stated` holds
    what later steps read of this alert; its live delays join the key in `keep_stated`."""
    name = display_name(ctx.profile.name)
    return AlertEvent(
        profile_id=ctx.profile.id,
        profile_name=ctx.profile.name,
        direction=ctx.direction,
        service_date=ctx.schedule.key.service_date,
        leg_index=None,
        kind=kind,
        severity=severity,
        train=train,
        text=f"{name}: {text}",
        key=_event_key(ctx, kind, train, text if key_facts is None else key_facts),
        created_at=ctx.now,
        facts=stated or AlertFacts(),
    )


def find_next_good(
    s: OccurrenceState,
    ctx: Ctx,
    answer: ProviderAnswer,
    after: TrainId | None = None,
    station: str | None = None,
    kept: ClassifiedLeg | None = None,
    n_before: int = 0,
    not_before: datetime | None = None,
    earliest: bool = False,
) -> Connection | None:
    """The next good connection from `station` (default the origin), not starting on `after`
    and not leaving before `not_before`. Both only leave the ranking: identity is judged on the
    whole answer (6.1). A window day and an other boarding station (`earliest`) take the
    earliest good one; elsewhere the usual route wins."""
    at = station or boarding_origin(s, ctx)
    floor = None if not_before is None else to_utc(not_before)
    transfers = ctx.profile.journey(ctx.direction).transfers
    earliest = earliest or ctx.schedule.mode == "window" or at == s.boarding_station
    c, _ = choose_replacement(
        answer,
        usual_remaining=None if earliest else remaining_pattern(transfers, at),
        candidates=[
            c
            for c in answer.connections
            if (after is None or c.legs[0].train != after)
            and (floor is None or _departure(c) >= floor)
        ],
        **_good_kw(s, ctx, at, kept, n_before),
    )
    return c


def _departure(c: Connection) -> datetime:
    d = c.legs[0].board.departure
    t = d.value if d and d.value else c.legs[0].train.planned_departure
    return t.astimezone(UTC)


def auto_lock(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Transition:
    """Lock the usual connection from the window start (Q9 quiet early watch), unless a lock
    exists (7.2, 6.3). A window day locks the earliest good train in its window instead. After
    a release, or when the pick is on_leave, only a sign of leaving locks a train. A tap before
    a minimum stay ends still follows a train.
    A usual connection that is already broken is still locked: the follow step relocks it and
    sends the cancellation (7.4)."""
    unchanged = Transition(s, ())
    now = ctx.now.astimezone(UTC)
    if s.lock is not None or s.not_travelling:
        return unchanged
    if now < ctx.schedule.window_start.astimezone(UTC) and not s.travelling:
        return unchanged
    p, d = ctx.profile, ctx.direction
    q = answer.query
    if (q.origin, q.destination) != (boarding_origin(s, ctx), p.destination(d)):
        return unchanged
    if s.released or s.boarding_station or ctx.schedule.pick == "on_leave":
        # the user was never seen near the train let go, follows none until leaving, or stands
        # at an other station: the next train they can catch, never the usual one
        return _lock_after_sign(s, ctx, answer)
    if ctx.schedule.mode == "window":
        return _lock_earliest(s, ctx, answer)
    # provider failures raise, so any answer is healthy; one asked after the usual departure
    # cannot prove the usual is absent, so it is not judged (6.3, rule 2)
    if q.at.astimezone(UTC) > ctx.schedule.first_departure.astimezone(UTC):
        return unchanged
    usual = usual_connection(
        answer, p.origin(d), ctx.schedule.first_departure, p.journey(d).transfers, ctx.signature
    )
    if usual is not None:
        return _lock_usual(s, ctx, answer, usual)
    return _lock_without_usual(s, ctx, answer)


def _lock_usual(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, usual: Connection
) -> Transition:
    now = ctx.now.astimezone(UTC)
    events: list[AlertEvent] = []
    locked = present(  # no lock existed
        write_lock(
            s,
            journey_of(usual, LockSource.AUTO, Commitment.INTENDED, answer.fetched_at),
            expect=None,
            now=now,
        )
    )
    first = usual.legs[0]
    # the newest copy says whether it left: an older one may have won on arrival (6.2)
    cur = current_connection(evidence(answer), usual)
    if (
        _departure(cur) < now
        and classify_leg(cur.legs[0], first.alight.code).kind in _RUNNING_LEG_KINDS
        and not _waited_at(s, first.board.code, _departure(cur))
    ):
        # the user may be on it; show where to go if not (7.12 rule 7)
        nxt = find_next_good(s, ctx, answer, after=first.train)
        text = f"your usual {hhmm(ctx.schedule.first_departure)} has left. " + (
            f"If you are not on it, next good connection: {hhmm(_departure(nxt))}."
            if nxt is not None
            else "If you are not on it, no good connection found, check the NS app."
        )
        events.append(
            make_event(ctx, EventKind.NEXT_CONNECTION, Severity.SILENT, text, first.train)
        )
    # The first train has its note above.
    return _locked_tail(locked, ctx, answer, events, skip=first.train)


def _locked_tail(
    locked: OccurrenceState,
    ctx: Ctx,
    answer: ProviderAnswer,
    events: Sequence[AlertEvent] = (),
    skip: TrainId | None = None,
) -> Transition:
    """7.2: a train already broken when locked is relocked at once, with its warning, from this
    answer; departure progress comes first, so a train the user may already be on is warned
    about, never relocked away (7.4, rule 7). `skip`: a train whose left note is already told."""
    pool = merged_pool(evidence(answer), answer.query)  # every answer of the poll is evidence
    locked, left = _advance_all(locked, ctx, pool, live=True)
    out = [*events, *(e for e in left if skip is None or e.train != skip)]
    t = _follow(locked, ctx, pool, (answer.query,))
    return keep_stated(t.state, quiet_events(t.state, ctx, (*out, *t.events)))


def _lock_earliest(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Transition:
    """Window mode: the earliest good train from the window start (or the stay end) up to the
    latest time. A user already travelling gets the next train they can catch, and so does a
    day whose stay was capped at the latest time."""
    sch = ctx.schedule
    last = present(sch.last_departure)
    floor = None if s.travelling else sch.first_departure
    capped = to_utc(sch.first_departure) >= to_utc(last)
    c = find_next_good(s, ctx, answer, not_before=floor)
    if c is None or (not s.travelling and not capped and _departure(c) > to_utc(last)):
        if s.missing_sent:
            return Transition(s, ())
        start = hhmm(sch.first_departure)
        text = f"no train found from {start} to {hhmm(last)}, check the NS app."
        event = make_event(ctx, EventKind.NO_CONNECTION, Severity.LOUD, text)
        return Transition(replace(s, missing_sent=True), (event,))
    new = journey_of(c, LockSource.AUTO, Commitment.INTENDED, answer.fetched_at)
    locked = present(write_lock(s, new, expect=None, now=ctx.now.astimezone(UTC)))
    return _locked_tail(locked, ctx, answer)


def _lock_after_sign(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Transition:
    """No train until the user is seen leaving: then the next one they can catch. A station or
    a leave of the origin place locks by itself (`on_location`); this covers the tap."""
    if not s.travelling:
        return Transition(s, ())
    c = find_next_good(s, ctx, answer)
    if c is None:
        return Transition(s, ())
    new = journey_of(c, LockSource.AUTO, Commitment.INTENDED, answer.fetched_at)
    s = replace(s, released=False)
    locked = present(write_lock(s, new, expect=None, now=ctx.now.astimezone(UTC)))
    return _locked_tail(locked, ctx, answer)


def _lock_without_usual(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Transition:
    events: list[AlertEvent] = []
    if not s.missing_sent:
        events.append(
            make_event(
                ctx,
                EventKind.MISSING,
                Severity.LOUD,
                f"your usual {hhmm(ctx.schedule.first_departure)} is not in NS data.",
            )
        )
        s = replace(s, missing_sent=True)
    nxt = find_next_good(s, ctx, answer)
    if nxt is None:
        text = "no connection found, check the NS app."
        events.append(make_event(ctx, EventKind.NO_CONNECTION, Severity.LOUD, text))
        return Transition(s, tuple(events))
    locked = present(
        write_lock(
            s,
            replace(journey_of(nxt, LockSource.AUTO, Commitment.INTENDED), plan=None),
            expect=None,
            now=ctx.now.astimezone(UTC),
        )
    )
    return Transition(locked, tuple(events))


def _ridden_source(s: OccurrenceState, train: TrainId, now: datetime | None) -> Journey | None:
    """The lock or retained remainder whose first unfinished leg is `train`, a train the user
    may be on. A retained remainder holds such a train by construction, whatever its state."""
    if rides(s.lock, train, now):
        return s.lock
    return next((j for j in s.retained if (f := unfinished_leg(j)) and f.train == train), None)


def whole_day_keys(p: Profile, key: OccurrenceKey) -> tuple[OccurrenceKey, ...]:
    """Both directions' occurrence keys of the commute day `key` belongs to: the caller applies
    a whole-day `NotTravelling` to each (7.8, 7.13), also to one whose window has not opened."""
    commute_day = key.service_date - timedelta(days=p.journey(key.direction).day_offset)
    return tuple(
        OccurrenceKey(key.profile_id, d, commute_day + timedelta(days=p.journey(d).day_offset))
        for d in Direction
    )


def target_occurrence(
    states: Sequence[OccurrenceState], action: UserAction
) -> OccurrenceState | None:
    """Which occurrence a user action applies to (7.13)."""
    if action.service_date is not None:
        # a resolved occurrence takes no `choose` or `leaving`: nothing would follow it (rule 6)
        return next(
            (
                x for x in states if x.key.service_date == action.service_date
                and (isinstance(action, NotTravelling) or not is_resolved(x))
            ),
            None,
        )  # fmt: skip
    open_ = sorted((x for x in states if not is_resolved(x)), key=lambda x: x.key.service_date)
    if isinstance(action, NotTravelling):
        if open_ or not action.whole_day:
            return next(iter(open_), None)
        # whole day after a skipped direction (the other one may not exist yet): the newest
        # occurrence names the commute day, and the caller fans out to both directions (7.8)
        return max(states, key=lambda x: x.key.service_date, default=None)
    if isinstance(action, Choose):
        first = action.connection.legs[0].train
        # no clock here: a lock leg counts by its state (the user "rides" it, 7.13)
        riding = next((x for x in open_ if _ridden_source(x, first, None) is not None), None)
        if riding is not None:
            return riding
    # an occurrence whose main journey is done (only a retained remainder runs) is not "not yet
    # riding": a new choice or tap is not for it, unless it names the ridden train (above)
    return next(
        (x for x in open_ if x.direction_state not in (DirectionState.RIDING, DirectionState.DONE)),
        None,
    )


def _refuse(s: OccurrenceState, ctx: Ctx, reason: str) -> Transition:
    text = f"choice refused: {reason}."
    return Transition(s, (make_event(ctx, EventKind.CHOOSE_REFUSED, Severity.LOUD, text),))


def _missed_head(s: OccurrenceState, source: Journey | None) -> tuple[LegProgress, ...]:
    """A missed-transfer remainder starts at the transfer: the lock legs into it are its past."""
    if source is None or source is s.lock or s.lock is None:
        return ()
    at = source.legs[0].board_station
    alights = [i for i, x in enumerate(s.lock.legs) if x.alight_stop == at]
    return s.lock.legs[: alights[0] + 1] if alights else ()


def _choice_check(
    s: OccurrenceState,
    ctx: Ctx,
    pool: Pool,
    c: Connection,
    source: Journey | None,
    head: Sequence[LegProgress],
    done: Sequence[LegProgress],
) -> GoodCheck:
    p, d = ctx.profile, ctx.direction
    if source is not None and len(c.legs) == 1:
        if c.destination != p.destination(d):
            return GoodCheck(False, "does not arrive at the destination")
        if len(head) + len(done) > max_transfers(p.journey(d).transfers):
            return GoodCheck(False, "over the transfer cap")
        return GoodCheck(True, "ok")
    # a train the user may be on is kept: catchability applies only to the legs after it,
    # and the change from it (or from the last done leg) is checked like any other (rule 1)
    rest = c if source is None else Connection(c.legs[1:], c.status, c.ctx_recon)
    if source is not None:
        ridden = replace(source.legs[len(done)], intended_alight=rest.origin)
        kept, station = observe(pool, ridden)[0], rest.origin
        n_before = len(head) + len(done) + 1
    else:
        kept = observe(pool, done[-1])[0] if done else None
        n_before = len(done)
        station = done[-1].alight_stop if done else boarding_origin(s, ctx)
    if n_before and kept is None:
        return GoodCheck(False, "the change from your current train is not in NS data")
    return check_good(rest, pool, **_good_kw(s, ctx, station, kept, n_before))


def _keep_progress(c: Connection, source: Journey) -> tuple[LegProgress, ...]:
    """The ridden prefix keeps its progress, to the chosen stop."""
    have = {x.train: x for x in source.legs}

    def reuse(h: LegProgress, stop: str) -> LegProgress:
        if h.intended_alight == stop:
            return h
        return replace(_move_stop(h, stop, stop), intended_alight=stop)

    return tuple(
        progress_of(y) if (h := have.get(y.train)) is None else reuse(h, y.alight.code)
        for y in c.legs
    )


def _choose(s: OccurrenceState, ctx: Ctx, action: Choose) -> Transition:
    c = action.connection
    if not action.answers:
        return _refuse(s, ctx, "no fresh NS answer to check it against")
    pool = merged_pool(action.answers)
    source = _ridden_source(s, c.legs[0].train, ctx.now)
    # the kept prefix comes from the journey the choice continues, never from another one
    base = s.lock if source is None else source
    done = () if base is None else base.legs[: first_open(base)]
    head = _missed_head(s, source)
    check = _choice_check(s, ctx, pool, c, source, head, done)
    if not check.ok:
        return _refuse(s, ctx, check.reason)
    riding = source is not None or s.travelling
    new = journey_of(
        c,
        LockSource.CHOOSE,
        Commitment.COMMITTED if riding else Commitment.INTENDED,
        max(a.fetched_at for a in action.answers),
    )
    legs = new.legs if source is None else _keep_progress(c, source)
    new = replace(new, legs=(*head, *done, *legs))
    # a choice ends every retained remainder; the writer retains the old lock if ridden
    # a choice always writes over the lock it read
    written = present(write_lock(replace(s, retained=()), new, expect=s.lock, now=ctx.now))
    if source is not None:  # a ridden journey, also one promoted after the main one ended
        written = replace(written, direction_state=DirectionState.RIDING)
    return Transition(written, ())


def apply_action(s: OccurrenceState, ctx: Ctx, action: UserAction) -> Transition:
    if isinstance(action, Choose):
        return _choose(s, ctx, action)
    if isinstance(action, SetPrompt):  # not a plan fact: polls in flight stay valid
        return Transition(replace(s, prompt=action.prompt), ())
    if isinstance(action, ConfirmStation):
        return Transition(bumped(s, _confirm_station(s, action.station)), ())
    gen = s.generation + 1
    if isinstance(action, Leaving):
        lock = None if s.lock is None else replace(s.lock, commitment=Commitment.COMMITTED)
        return Transition(
            replace(
                s,
                travelling=True,
                tap_at=action.at,
                lock=lock,
                errand_deadline=None,
                generation=gen,
            ),
            (),
        )
    # the only silencer (7.8); retained remainders end with it (7.12 rule 7)
    return Transition(
        replace(
            s,
            not_travelling=True,
            skipped_direction_state=(
                s.skipped_direction_state if s.not_travelling else s.direction_state
            ),
            direction_state=DirectionState.EXPIRED,
            retained=(),
            generation=gen,
        ),
        (),
    )


def _confirm_station(s: OccurrenceState, station: str) -> OccurrenceState:
    """The user stands at `station`. A lock whose first train boards elsewhere and was not ridden
    goes; the next poll or the user's pick follows a train from there. The question stays open
    until a train is chosen, so a failed choice can be tried again."""
    s = replace(
        s,
        boarding_station=station,
        at_station=True,
        travelling=True,
        errand_deadline=None,
    )
    lock = s.lock
    if lock is None or lock.legs[0].board_station == station:
        return s
    if lock.legs[0].state in _DEPARTED:
        return s  # already riding: the station is where a later choice boards
    return replace(s, lock=None, direction_state=DirectionState.IDLE)


# --- follow, relock, restore (7.4, 7.12 rules 2, 3, 8, 21) ---

_RUNNING_LEG_KINDS = (LegKind.OK, LegKind.DELAYED)
AT_RISK_LOUD_MIN = 30
PLATFORM_LOUD_MIN = 15
_KEEP_POLLS = 20  # shortcut: health reads only the latest records; keep more if it ever needs a day


@dataclass(frozen=True, slots=True)
class PollAnswers:
    answers: tuple[ProviderAnswer, ...]  # healthy answers only: a failed request raised
    records: tuple[PollRecord, ...]
    started_generation: int


def first_open(j: Journey) -> int:
    return next((i for i, x in enumerate(j.legs) if x.state is not LegState.DONE), len(j.legs))


def _observed(x: LegProgress, now: datetime) -> bool:
    """Unfinished, or done and still watched; an earlier leg's watch ends with the last (rule 6)."""
    return x.observed_at(now)


def _restorable(s: OccurrenceState, ctx: Ctx, h: LegProgress) -> bool:
    """Not yet departed minus the catchability margin (rule 2): the buffer at the origin, else
    none (the user is on the way and the kept leg's transfer check decides)."""
    if h.board_station == boarding_origin(s, ctx):
        nb = catch_from_state(s, ctx.profile.journey(ctx.direction).buffer_min, ctx.now).not_before
    else:
        nb = ctx.now.astimezone(UTC)
    return h.train.planned_departure.astimezone(UTC) >= nb


def _late_onward(x: LegProgress, nxt: LegProgress | None) -> datetime | None:
    """`x.onward_from` when it is after the planned departure of the next leg not done."""
    t = x.onward_from
    if t is None or nxt is None or nxt.state is LegState.DONE:
        return None
    return t if t.astimezone(UTC) > nxt.train.planned_departure.astimezone(UTC) else None


def _proof_after(fresh: datetime | None, asked: datetime | None) -> datetime | None:
    """Only an answer asked from the station at or after this proves there is no connection;
    the time the last poll asked for counts, so a drifting delay does not keep it quiet."""
    if fresh is None or asked is None:
        return fresh
    return min(fresh, asked, key=to_utc)


def queries_needed(s: OccurrenceState, ctx: Ctx) -> list[Query]:
    """Rule 2. Each query carries a full date-time, so a leg after midnight is asked on its own
    date (7.13). The first query is from the first leg not done, else the usual (6.3); every
    retained remainder is asked for also without a lock (rule 7)."""
    p, d = ctx.profile, ctx.direction
    dest = p.destination(d)
    out: list[Query] = []
    seen: set[tuple[str, datetime]] = set()

    def add(station: str, at: datetime) -> None:
        key = (station, to_utc(at))  # local equality would merge the two 02:30s of the DST fold
        if key not in seen:
            seen.add(key)
            out.append(Query.for_profile(p, station, dest, at))

    window = ctx.schedule.mode == "window"
    origin = boarding_origin(s, ctx)
    if s.lock is None and (
        s.released
        or s.boarding_station
        or ctx.schedule.pick == "on_leave"
        or window
        and s.travelling
    ):
        # a later train, from now, for a station or leave event; a window day's tap before its
        # minimum stay ends, and a user at an other station, ask from now too
        early = window and s.travelling or s.boarding_station is not None
        start = ctx.now if early else max(ctx.schedule.first_departure, ctx.now, key=to_utc)
        add(origin, start)
    elif s.lock is None and window:  # the earliest still catchable: NS omits trains before
        add(origin, max(ctx.schedule.first_departure, ctx.now, key=to_utc))
    elif s.lock is None:  # the usual at its effective departure, also when past (6.3)
        add(origin, ctx.schedule.first_departure)
    else:
        k = first_open(s.lock)
        if k < len(s.lock.legs):
            add(s.lock.legs[k].board_station, s.lock.legs[k].train.planned_departure)
    quiet = quiet_watch(s, ctx)
    for j in (*((s.lock,) if s.lock else ()), *s.retained):
        k = first_open(j)
        skip = quiet and j is s.lock  # Q9: the origin answer alone, as before a lock
        for i, x in enumerate(j.legs):
            # k - 1: the next transfer's incoming leg
            if not skip and (_observed(x, ctx.now) or i == k - 1):
                add(x.board_station, x.train.planned_departure)
                if x.alight_stop != x.intended_alight:  # recovery search from a served endpoint
                    add(x.alight_stop, x.onward_from or x.train.planned_departure)
            nxt = j.legs[i + 1] if i + 1 < len(j.legs) else None
            late = _late_onward(x, nxt)
            if late is not None:  # a delay broke the change: search after the expected arrival
                add(x.alight_stop, late)
    for h in s.cancellation_history:
        if _restorable(s, ctx, h):
            add(h.board_station, h.train.planned_departure)
    return out


def merged_pool(answers: Sequence[ProviderAnswer], query: Query | None = None) -> Pool:
    """All answers plus every connection's suffixes, so a search from a transfer station can use
    an answer asked from earlier on the route (rule 3)."""
    conns = [c for a in answers for c in a.connections]
    frags = [Connection(c.legs[i:], c.status, None) for c in conns for i in range(1, len(c.legs))]
    newest = max(answers, key=lambda a: to_utc(a.fetched_at))
    return Pool(query or answers[0].query, (*conns, *frags), newest.fetched_at, tuple(answers))


def observe(
    pool: Pool, x: LegProgress
) -> tuple[ClassifiedLeg | None, Observation, datetime | None]:
    """The leg's classification, its observation over the whole poll, and when the answer it
    was read from was fetched."""
    leg, ob = find_leg(pool, x.train, x.board_station)
    if leg is None:
        return None, ob, None
    # find_leg found the leg in this pool, and every fragment comes from a raw connection of
    # one of its answers
    seen, best = present(current_run(pool.answers, leg.train, x.board_station, x.intended_alight))
    if best.leg.alternative_transport and best.kind is not LegKind.CANCELLED:
        # 6.3: a train NS now runs as other transport is no train to follow; NS itself
        # usually shows that as a cancellation, so it alerts and relocks as one
        best = replace(
            best, kind=LegKind.CANCELLED, dep_delay_min=0, arr_delay_min=None, last_served=None
        )
    return replace(best, observation=ob), ob, seen


def _name(x: LegProgress) -> str:
    return f"{hhmm(x.train.planned_departure)} {x.board_station} to {x.intended_alight}"


def _good_kw(
    s: OccurrenceState, ctx: Ctx, station: str, kept: ClassifiedLeg | None, n_before: int
) -> GoodKwargs:
    """`n_before` legs stay before the new suffix; the last of them is `kept`, which every
    caller must have observed when there is one (rule 1)."""
    if n_before and kept is None:
        raise AssertionError("a kept leg's change must be checked")
    p, d = ctx.profile, ctx.direction
    j = p.journey(d)
    now = ctx.now
    if not (s.travelling or s.at_station) and quiet_watch(s, ctx):
        # Q9: a lock made at the window start offers no train earlier than the active point's reach
        now = active_at(s, ctx)
    catch = (
        catch_from_state(s, j.buffer_min, now)
        if kept is None
        else Catchability(ctx.now.astimezone(UTC))
    )
    return GoodKwargs(
        board_station=station,
        destination=p.destination(d),
        catch=catch,
        kept_incoming=kept,
        done_transfers=max(n_before - 1, 0),
        max_transfers=max_transfers(j.transfers),
        profile_min=p.transfer_min,
    )


def _journey(
    lock: Journey, prefix: Sequence[LegProgress], tail: Sequence[Leg], source: LockSource
) -> Journey:
    arrival = tail[-1].alight.planned_arrival if tail else None
    legs = (*prefix, *(progress_of(x) for x in tail))
    return Journey(legs, source, lock.commitment, arrival or lock.planned_final_arrival)


def _progress(s: OccurrenceState, legs: Sequence[LegProgress]) -> OccurrenceState:
    """Observed progress only; the generation is raised once per poll by `bumped`."""
    return replace(s, lock=replace(present(s.lock), legs=tuple(legs)))


def _write(s: OccurrenceState, new: Journey) -> OccurrenceState:
    # None: every caller has checked that the replaced train is not ridden
    # relock and restore pass the lock just read as `expect`
    return present(write_lock(s, new, expect=s.lock, now=None))


def _move_stop(x: LegProgress, end: str, stop: str) -> LegProgress:
    """A new served endpoint and alighting stop; an accepted arrival at another stop is no
    longer the arrival, so the leg is followed again (rule 6)."""
    return x.move_endpoint(end, stop)


def _restore(s: OccurrenceState, ctx: Ctx, pool: Pool) -> Transition | None:
    """Rule 8: a reinstated train moves back only for legs not boarded and only if good."""
    for h in s.cancellation_history:
        if not _restorable(s, ctx, h):
            continue
        for j in range(len(present(s.lock).legs)):
            t = _restore_at(s, ctx, pool, h, j)
            if t is not None:
                return t
    return None


def _restore_at(
    s: OccurrenceState, ctx: Ctx, pool: Pool, h: LegProgress, j: int
) -> Transition | None:
    """Train `h` back in the journey from leg `j`, when that leg boards where `h` does."""
    lock = present(s.lock)
    p, d = ctx.profile, ctx.direction
    legs = lock.legs
    station = boarding_origin(s, ctx) if j == 0 else legs[j - 1].intended_alight
    rest = legs[j:]
    if station != h.board_station or rest[0].train == h.train:
        return None
    if any(y.state is LegState.DONE or may_be_on(y, ctx.now) for y in rest):
        return None
    kept = observe(pool, legs[j - 1])[0] if j else None
    if j and kept is None:
        return None
    kw = _good_kw(s, ctx, station, kept, j)
    good = [
        c for c in pool.connections if c.legs[0].train == h.train and check_good(c, pool, **kw).ok
    ]
    if not good:
        return None
    usual = remaining_pattern(p.journey(d).transfers, station)
    c = next((c for c in good if matches_pattern(c, usual)), good[0])
    prefix = list(legs[:j])
    if j:  # the kept leg reaches the station again
        prefix[-1] = _move_stop(prefix[-1], station, station)
    written = _write(s, _journey(lock, prefix, c.legs, LockSource.RESTORE))
    history = tuple(y for y in written.cancellation_history if y.train != h.train)
    text = f"back to normal: your {_name(h)} train runs again."
    return Transition(
        replace(written, cancellation_history=history),
        (make_event(ctx, EventKind.RESTORED, Severity.SILENT, text, h.train),),
    )


class Cause(NamedTuple):
    """What a relock reports, whether or not it moves anything."""

    kind: EventKind
    severity: Severity
    text: str
    train: TrainId


@dataclass(frozen=True, slots=True, kw_only=True)
class Relock:
    """Replace `legs[first_replaced:]` by the next good connection from `station` (7.4). `over_cap`
    is the actual route of the boarded train before them, followed when nothing good fits (6.3).
    `since`: only an answer asked from `station` at or after it proves there is no connection.
    `history`: the train being replaced, remembered so it can be restored (rule 8).
    `at_station`: the user stands at `station` (Q1): the clause states the replacement's status,
    so the miss and the at-station message are one. `leave_by`: the user is still at the origin
    place (Q3): the clause names the next train they can catch and when to leave, and finding
    none is told in the same one message."""

    legs: Sequence[LegProgress]
    first_replaced: int
    station: str
    kept: ClassifiedLeg | None
    cause: Cause
    history: LegProgress | None = None
    over_cap: Connection | None = None
    since: datetime | None = None
    source: LockSource = LockSource.RELOCK
    at_station: bool = False
    leave_by: bool = False


def _follow_over_cap(s: OccurrenceState, ctx: Ctx, cut: Relock, over_cap: Connection) -> Transition:
    """No good connection within the usual changes: follow what NS shows, and say so."""
    legs, j = cut.legs, cut.first_replaced
    p, d = ctx.profile, ctx.direction
    kind, sev, text, train = cut.cause
    end = over_cap.legs[0].alight.code
    ride = _move_stop(legs[j - 1], end, end)
    new = _journey(present(s.lock), (*legs[: j - 1], ride), over_cap.legs[1:], LockSource.RELOCK)
    over = j - 1 + len(over_cap.legs) - 1 > max_transfers(p.journey(d).transfers)
    via = ", ".join(over_cap.transfer_stations) or end
    note = f"no good connection within your usual changes; following NS via {via}" + (
        ", the extra legs are beyond the sensors." if over else "."
    )
    reroute = make_event(ctx, EventKind.ROUTE_CHANGED, Severity.LOUD, f"{text}: {note}", train)
    if kind is EventKind.ROUTE_CHANGED:
        return Transition(_write(s, new), (reroute,))
    return Transition(_write(s, new), (make_event(ctx, kind, sev, f"{text}.", train), reroute))


def _relock(
    s: OccurrenceState, ctx: Ctx, pool: Pool, asked: tuple[Query, ...], cut: Relock
) -> Transition:
    """A kept leg legs[j - 1] that is not in the answers cannot have its change checked (rule 1):
    the cause is still reported, and nothing is relocked on it."""
    legs, j, station, kept = cut.legs, cut.first_replaced, cut.station, cut.kept
    history = cut.history
    lock = present(s.lock)
    p, d = ctx.profile, ctx.direction
    kind, sev, text, train = cut.cause
    if j and kept is None:
        note = f"{text}; the change from your current train is not in NS data, check the NS app."
        return Transition(_progress(s, legs), (make_event(ctx, kind, sev, note, train),))
    usual = remaining_pattern(p.journey(d).transfers, station)
    # an older answer's running copy of the train being replaced is no replacement for it
    cands = [c for c in pool.connections if history is None or c.legs[0].train != history.train]
    c, how = choose_replacement(
        pool, usual_remaining=usual, candidates=cands, **_good_kw(s, ctx, station, kept, j)
    )
    if c is None and cut.over_cap is not None:
        return _follow_over_cap(s, ctx, cut, cut.over_cap)
    if c is None and cut.leave_by:
        none = f"{text}. No train you can still catch was found, check the NS app."
        return Transition(_progress(s, legs), (make_event(ctx, kind, sev, none, train),))
    if c is None:
        events = [make_event(ctx, kind, sev, f"{text}.", train)]
        if any(
            q.origin == station and (cut.since is None or to_utc(q.at) >= to_utc(cut.since))
            for q in asked
        ):
            nothing = "no connection found, check the NS app."
            events.append(make_event(ctx, EventKind.NO_CONNECTION, Severity.LOUD, nothing))
        return Transition(_progress(s, legs), tuple(events))
    via = ", ".join(c.transfer_stations) or "a direct train"
    # the replacement by identity, not by its live time: a drifting estimate is not news (7.5)
    # and its stated time by its delay, which `keep_stated` turns into 3-min news steps
    late = (_departure(c) - to_utc(c.legs[0].train.planned_departure)) // timedelta(minutes=1)
    named = tuple(train_ident(x.train) for x in c.legs)
    key_facts = f"{text}|" + ",".join(named)
    late_now = classify_leg(c.legs[0], c.legs[0].alight.code).dep_delay_min
    route, crowd = (f", new route via {via}" if how == "new_route" else ""), crowd_text(c.legs)
    if cut.at_station:
        clause = f" Next: {status_text(c.legs, late_now)}."
    elif cut.leave_by:  # the crowd note stays last: the ledger strips and re-appends it there
        leave = hhmm(_departure(c) - timedelta(minutes=p.journey(d).buffer_min))
        clause = f" Next you can catch: {hhmm(_departure(c))}{route}. Leave by {leave}{crowd}."
    else:
        clause = f" Next good connection: {hhmm(_departure(c))}{route}{crowd}."
    written = _write(s, _journey(lock, legs[:j], c.legs, cut.source))
    if history is not None and history.train not in {y.train for y in s.cancellation_history}:
        written = replace(written, cancellation_history=(*s.cancellation_history, history))
    stated = AlertFacts(
        delays=(LiveDelay("", late),),
        named=named,
        next_clause=clause,
        crowd_note=crowd,
    )
    return Transition(
        written, (make_event(ctx, kind, sev, f"{text}.{clause}", train, key_facts, stated),)
    )


def _onward(
    p: Profile, x: LegProgress, cl: ClassifiedLeg, ncl: ClassifiedLeg | None
) -> datetime | None:
    """Expected arrival at the alighting stop plus the transfer minimum: the earliest onward
    departure, for every kind of break (7.4). UTC arithmetic keeps the DST fold (7.13)."""
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop and served(st)), None)
    arr = None if stop is None else stop.arrival
    t = None if arr is None else arr.value or arr.planned
    if t is None:
        return None
    msg = None if ncl is None else ncl.leg.transfer_message_min
    minimum = transfer_minimum(x.alight_stop, p.transfer_min, cl.leg.transfer_time_to_next, msg)
    return (to_utc(t) + timedelta(minutes=minimum)).astimezone(TZ)


def _at_risk(ctx: Ctx, x: LegProgress, nxt: LegProgress, reason: str) -> AlertEvent:
    soon = to_utc(nxt.train.planned_departure) - to_utc(ctx.now) <= timedelta(
        minutes=AT_RISK_LOUD_MIN
    )
    text = f"your change at {x.alight_stop} is at risk: {reason}."
    sev = Severity.LOUD if soon else Severity.SILENT
    return make_event(ctx, EventKind.TRANSFER_AT_RISK, sev, text, nxt.train)


def _limit_at(limit: time, planned: datetime) -> datetime:
    """`latest_acceptable_arrival` as an instant: the one nearest the planned arrival, so a
    23:55 limit for a 23:50 arrival stays on that evening when the train slips past midnight."""
    day = planned.astimezone(TZ).date()
    options = [resolve_local(day + timedelta(days=n), limit, edge="start") for n in (-1, 0, 1)]
    return min(options, key=lambda t: abs(to_utc(t) - to_utc(planned)))


def _past_limit(ctx: Ctx, arr: datetime | None, planned: datetime | None) -> bool:
    limit = ctx.profile.alerts.latest_acceptable_arrival
    return (
        ctx.direction is Direction.OUTBOUND
        and limit is not None
        and arr is not None
        and to_utc(arr) > to_utc(_limit_at(limit, planned or arr))
    )


def delayed_event(ctx: Ctx, x: LegProgress, cl: ClassifiedLeg, *, last: bool) -> AlertEvent:
    """7.9 `delayed`: silent, loud when the last leg of the outbound journey arrives after
    `latest_acceptable_arrival` (compared as instants, 7.13). A broken change is its own loud
    event. Departure and arrival delays are each stepped by `keep_stated`, so only a 3-min move of
    either is news, and a fall back to on time becomes its recovery message (7.5)."""
    dep, arr_late = cl.dep_delay_min, cl.arr_delay_min
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop), None)
    arr = None if stop is None or stop.arrival is None else stop.arrival.value
    planned = None if stop is None or stop.arrival is None else stop.arrival.planned
    past = last and _past_limit(ctx, arr, planned)
    if dep is not None and arr_late is not None and dep != arr_late:
        on = "on time" if arr_late <= ON_TIME_MAX_MIN else f"{arr_late} min late"
        text = f"your {_name(x)} train leaves {dep} min late and arrives {on}"
    else:
        text = (
            f"your {_name(x)} train is delayed {arr_late if arr_late is not None else dep or 0} min"
        )
    if arr is not None:
        text += f", arriving {hhmm(arr)}"
    sev = Severity.LOUD if past else Severity.SILENT
    stated = AlertFacts(
        delays=(LiveDelay("d", dep), LiveDelay("a", arr_late)),
        subject=f"your {_name(x)}",
        crowd_note=crowd_text([cl.leg]),
    )
    return make_event(
        ctx, EventKind.DELAYED, sev, text + crowd_text([cl.leg]) + ".", x.train, _name(x), stated
    )


def _platform(ctx: Ctx, x: LegProgress, cl: ClassifiedLeg) -> AlertEvent | None:
    """7.9 `platform_change` for a leg not yet left: loud within 15 min of its expected
    departure, else silent (escalated once it enters the zone, rule 4)."""
    b = cl.leg.board
    if not b.planned_track:
        return None
    track = b.actual_track or b.planned_track  # no live track means the planned one
    soon = _departs(x, cl) - to_utc(ctx.now) <= timedelta(minutes=PLATFORM_LOUD_MIN)
    sev = Severity.LOUD if soon else Severity.SILENT
    planned = track == b.planned_track
    if planned:  # `keep_stated` keeps it only after an announced change
        text = f"your {_name(x)} train leaves from platform {b.planned_track} again, as planned."
    else:
        text = f"your {_name(x)} train now leaves from platform {track} (was {b.planned_track})."
    key_facts = f"track {track}" + ("|planned" if planned else "")
    slot = "moved#" + _event_key(ctx, EventKind.PLATFORM_CHANGE, x.train, "track")
    stated = AlertFacts(moved_slot=slot, back_on_plan=planned)
    return make_event(ctx, EventKind.PLATFORM_CHANGE, sev, text, x.train, key_facts, stated)


def _crowd(ctx: Ctx, x: LegProgress, cl: ClassifiedLeg) -> AlertEvent | None:
    level = cl.leg.crowd_forecast
    if level is None or cl.leg.alternative_transport or _departs(x, cl) <= to_utc(ctx.now):
        return None
    slot = "crowd#" + _event_key(ctx, EventKind.CROWD_CHANGE, x.train, "forecast")
    return make_event(
        ctx,
        EventKind.CROWD_CHANGE,
        Severity.SILENT,
        f"your {_name(x)} train has a {level} crowd forecast per NS.",
        x.train,
        level,
        AlertFacts(crowd_slot=slot, crowd_forecast=level),
    )


@dataclass(frozen=True, slots=True)
class _Watch:
    """What every check of one `_follow` reads. The legs are passed apart: `_follow` updates them."""

    s: OccurrenceState
    ctx: Ctx
    pool: Pool
    asked: tuple[Query, ...]
    lock: Journey
    obs: Sequence[ClassifiedLeg | None]
    rc: Connection | None  # the route NS now gives the first open leg


class _Proposal(NamedTuple):
    """A move a check asks for. `_follow` alone decides whether it is taken (7.4)."""

    transition: Transition
    ridden: bool = False  # the broken train may carry the user: warn, never move


def _boarded_reroute(
    w: _Watch, legs: Sequence[LegProgress], first_replaced: int
) -> Connection | None:
    """The actual route of the boarded kept leg just before `first_replaced`, if NS reroutes it."""
    before = first_replaced - 1
    boarded = first_replaced > 0 and may_be_on(legs[before], w.ctx.now)
    on_it = w.rc is not None and boarded and w.rc.legs[0].train == legs[before].train
    return w.rc if on_it else None


def _since_change(w: _Watch, legs: Sequence[LegProgress], first_replaced: int) -> datetime | None:
    before = first_replaced - 1
    return _proof_after(
        _late_onward(legs[before], legs[first_replaced]),
        _late_onward(w.lock.legs[before], w.lock.legs[first_replaced]),
    )


def _check_cancelled(
    w: _Watch, legs: Sequence[LegProgress], i: int, cl: ClassifiedLeg
) -> _Proposal | None:
    x = legs[i]
    if cl.kind is not LegKind.CANCELLED or x.state is LegState.DONE:
        return None
    text = f"your {_name(x)} train is cancelled"
    t = _relock(
        w.s,
        w.ctx,
        w.pool,
        w.asked,
        Relock(
            legs=legs,
            first_replaced=i,
            station=x.board_station,
            kept=w.obs[i - 1] if i else None,
            cause=Cause(EventKind.CANCELLED, Severity.LOUD, text, x.train),
            history=x,
            since=_since_change(w, legs, i) if i else None,
        ),
    )
    return _Proposal(t, ridden=x.state is LegState.RIDING)


def _is_cut(x: LegProgress, cl: ClassifiedLeg) -> bool:
    return cl.kind is LegKind.CUT_SHORT or x.alight_stop != x.intended_alight


def _served_end(x: LegProgress, cl: ClassifiedLeg) -> str:
    if cl.last_served is not None:
        return cl.last_served.code
    return x.intended_alight if cl.kind in _RUNNING_LEG_KINDS else x.served_endpoint


def _observed_ends(
    p: Profile,
    x: LegProgress,
    nxt: LegProgress | None,
    cl: ClassifiedLeg,
    ncl: ClassifiedLeg | None,
) -> LegProgress:
    """The leg with what this answer shows of its endpoint and its earliest onward departure."""
    if _is_cut(x, cl):
        end = _served_end(x, cl)
        # frozen once a recovery leg is boarded, by its departure evidence (7.4)
        frozen = x.endpoint_phase is EndpointPhase.FROZEN or (
            nxt is not None and nxt.state in _DEPARTED
        )
        x = replace(_move_stop(x, end, x.alight_stop if frozen else end), alight_frozen=frozen)
    onward = _onward(p, x, cl, ncl)
    return x if onward is None else replace(x, onward_from=onward)


def _check_cut_short(
    w: _Watch, legs: Sequence[LegProgress], i: int, before: LegProgress, cl: ClassifiedLeg
) -> _Proposal | None:
    """`before` is the leg as it was before this answer's endpoint was applied to `legs[i]`."""
    x = legs[i]
    if not _is_cut(before, cl) or x.endpoint_phase is EndpointPhase.FROZEN:
        return None
    nxt = legs[i + 1] if i + 1 < len(legs) else None
    end = x.served_endpoint
    dest = w.ctx.profile.destination(w.ctx.direction)
    if cl.kind is LegKind.CUT_SHORT:
        text = f"your {_name(x)} train runs only to {end}, change there"
        cause = Cause(EventKind.CUT_SHORT, Severity.LOUD, text, x.train)
    else:
        text = f"back to normal: your {_name(x)} train reaches {end} again"
        cause = Cause(EventKind.RESTORED, Severity.SILENT, text, x.train)
    if nxt is not None and end == dest:  # extended to the destination again
        new = _journey(w.lock, legs[: i + 1], (), LockSource.RESTORE)
        note = make_event(w.ctx, cause.kind, cause.severity, f"{cause.text}.", cause.train)
        return _Proposal(Transition(_write(w.s, new), (note,)))
    if end == dest or (nxt is not None and nxt.board_station == end):
        return None
    original = nxt if nxt is not None and nxt.board_station == x.intended_alight else None
    asked_from = before.onward_from if before.alight_stop == end else None
    t = _relock(
        w.s,
        w.ctx,
        w.pool,
        w.asked,
        Relock(
            legs=legs,
            first_replaced=i + 1,
            station=end,
            kept=cl,
            cause=cause,
            history=original,
            over_cap=_boarded_reroute(w, legs, i + 1),
            since=_proof_after(x.onward_from, asked_from),
        ),
    )
    return _Proposal(t)


def _check_transfer(
    w: _Watch,
    legs: Sequence[LegProgress],
    i: int,
    cl: ClassifiedLeg,
    ncl: ClassifiedLeg | None,
) -> tuple[list[AlertEvent], _Proposal | None]:
    """The leg's delay and its change into the next leg. Every running leg reports: `keep_stated`
    keeps only news, including the fall back to on time."""
    x, ctx = legs[i], w.ctx
    nxt = legs[i + 1] if i + 1 < len(legs) else None
    late = cl.kind in _RUNNING_LEG_KINDS and x.state is not LegState.DONE
    if nxt is None or ncl is None or ncl.kind is LegKind.CANCELLED:
        # a cancelled next leg is the news, not this delay, and has its own turn as a cancellation
        return ([delayed_event(ctx, x, cl, last=nxt is None)] if late and ncl is None else []), None
    tc = check_transfer(cl, ncl, x.alight_stop, ctx.profile.transfer_min)
    if tc.state is TransferState.BROKEN:
        text = f"your change at {x.alight_stop} is broken: {tc.reason}"
        t = _relock(
            w.s,
            ctx,
            w.pool,
            w.asked,
            Relock(
                legs=legs,
                first_replaced=i + 1,
                station=x.alight_stop,
                kept=cl,
                cause=Cause(EventKind.TRANSFER_BROKEN, Severity.LOUD, text, nxt.train),
                over_cap=_boarded_reroute(w, legs, i + 1),
                since=_since_change(w, legs, i + 1),
            ),
        )
        return [], _Proposal(t, ridden=nxt.state in _DEPARTED)
    # after the break check: a broken change is its own loud event
    found = [delayed_event(ctx, x, cl, last=False)] if late else []
    # at risk warns before a change; once the onward train left, only broken is news
    if tc.state is TransferState.AT_RISK and nxt.state not in _DEPARTED:
        found.append(_at_risk(ctx, x, nxt, tc.reason))
    return found, None


def _check_route_change(
    w: _Watch, legs: Sequence[LegProgress], k: int, rc: Connection, *, boarded: bool
) -> _Proposal:
    x, ctx = legs[k], w.ctx
    via = ", ".join(rc.transfer_stations) or "a direct train"
    text = f"NS now routes your {_name(x)} train via {via}"
    cause = Cause(EventKind.ROUTE_CHANGED, Severity.LOUD, text, x.train)
    if boarded:  # `_follow` moved its stop to where NS changes: relock what follows from there
        cut = Relock(
            legs=legs,
            first_replaced=k + 1,
            station=x.alight_stop,
            kept=w.obs[k],
            cause=cause,
            over_cap=rc,
        )
        return _Proposal(_relock(w.s, ctx, w.pool, w.asked, cut))
    kept = w.obs[k - 1] if k else None
    # 6.3: follow the new route only within the cap and if good, else relock
    checkable = not k or kept is not None
    if checkable and check_good(rc, w.pool, **_good_kw(w.s, ctx, x.board_station, kept, k)).ok:
        new = _journey(w.lock, legs[:k], rc.legs, LockSource.RELOCK)
        note = make_event(ctx, cause.kind, cause.severity, f"{text}, following it.", x.train)
        return _Proposal(Transition(_write(w.s, new), (note,)))
    cut = Relock(legs=legs, first_replaced=k, station=x.board_station, kept=kept, cause=cause)
    return _Proposal(_relock(w.s, ctx, w.pool, w.asked, cut))


def _moved(t: Transition, events: Sequence[AlertEvent]) -> Transition:
    """A taken move. A platform of a train relocked away is no longer the user's platform."""
    kept = {y.train for y in t.state.lock.legs} if t.state.lock else set()
    mine = [e for e in events if e.kind is not EventKind.PLATFORM_CHANGE or e.train in kept]
    return Transition(t.state, (*mine, *t.events))


def _planned_route_change(open_legs: Sequence[LegProgress], pool: Pool) -> Connection | None:
    # a served-endpoint recovery is not the planned pattern, so it is not a route change
    if not all(x.alight_stop == x.intended_alight for x in open_legs):
        return None
    expected = tuple(x.alight_stop for x in open_legs[:-1])
    return route_changed([x.train for x in open_legs], pool, expected)


def _uncertain_change(nxt: LegProgress, *seen: Observation) -> bool:
    return nxt.state is not LegState.DONE and Observation.UNCERTAIN in seen


def _follow(
    s: OccurrenceState,
    ctx: Ctx,
    pool: Pool,
    asked: tuple[Query, ...],
    *,
    relock: bool = True,
) -> Transition:
    """The locked-journey checks (7.3, 7.4). A warning never depends on permission to move a
    train: a break at a train the user may be on, or anywhere in a retained remainder
    (`relock` False, rule 7), is reported with the next good connection and moves nothing;
    only observed progress (endpoints, onward search time) is kept.

    This loop alone owns `legs`, `events` and `relock`: each check proposes, and a move not
    taken keeps its warnings, while every later leg is still checked and reported but never
    relocked on top of the kept break (7.4)."""
    lock = present(s.lock)
    now = ctx.now
    legs = list(lock.legs)
    k = first_open(lock)
    seen = [observe(pool, x) for x in legs]
    obs = [o[0] for o in seen]
    rc = _planned_route_change(legs[k:], pool)
    w = _Watch(s, ctx, pool, asked, lock, obs, rc)
    events: list[AlertEvent] = []
    # from the incoming leg of the next transfer: a done leg stays watched, so its endpoint and
    # its change into the next leg are still checked (rule 6, lifecycle 26)
    for i in range(max(k - 1, 0), len(legs)):
        before, cl = legs[i], obs[i]
        if not _observed(before, now):
            continue
        nxt = legs[i + 1] if i + 1 < len(legs) else None
        ncl = obs[i + 1] if nxt is not None else None
        # 6.1, 7.3: an uncertain leg makes its change at risk, never fine, whatever progress says
        if nxt is not None and _uncertain_change(nxt, seen[i][1], seen[i + 1][1]):
            events.append(_at_risk(ctx, before, nxt, "a train is not reliably identified"))
        if cl is None:  # missing or uncertain: reported, never relocked on
            continue
        if ncl is not None:  # this change's own transfer fields, not another connection's
            cl, ncl = pair_transfer(pool.answers, cl, ncl)
        proposal = _check_cancelled(w, legs, i, cl)
        if proposal is None:
            usable = cl.kind in _RUNNING_LEG_KINDS or cl.kind is LegKind.CUT_SHORT
            if usable and before.state in MAY_HAVE_LEFT and (pc := _platform(ctx, before, cl)):
                events.append(pc)
            if usable and before.state in MAY_HAVE_LEFT and (crowd := _crowd(ctx, before, cl)):
                events.append(crowd)
            legs[i] = _observed_ends(ctx.profile, before, nxt, cl, ncl)
            proposal = _check_cut_short(w, legs, i, before, cl)
        if proposal is None:
            found, proposal = _check_transfer(w, legs, i, cl, ncl)
            events += found
        if proposal is None:
            continue
        if relock and not proposal.ridden:
            return _moved(proposal.transition, events)
        relock = False
        events += proposal.transition.events
    if rc is not None and obs[k] is not None:
        # a boarded train is never dropped: its stop moves to where NS changes, taken or not
        boarded = may_be_on(legs[k], now)
        if boarded:
            end = rc.legs[0].alight.code
            legs[k] = _move_stop(legs[k], end, end)
        proposal = _check_route_change(w, legs, k, rc, boarded=boarded)
        if relock:
            return _moved(proposal.transition, events)
        events += proposal.transition.events
    return Transition(_progress(s, legs), tuple(events))


def _departs(x: LegProgress, cl: ClassifiedLeg) -> datetime:
    """Expected departure, by the observed time when there is one."""
    d = cl.leg.board.departure
    t = d.value if d is not None and d.value is not None else x.train.planned_departure
    return to_utc(t)


def _advance(ctx: Ctx, pool: Pool, j: Journey) -> tuple[Journey, list[int]]:
    """Leg progress (7.7): boarding within the buffer of the expected departure, riding once it
    has departed. A train cancelled at its boarding stop is never boarded. A train not
    identified in this poll gets no progress: RIDING is departure evidence (it freezes an
    alighting stop, 7.4), while `may_be_on` already retains it by its planned time. Returns
    the journey and the indexes of the legs that departed now."""
    now = to_utc(ctx.now)
    buffer = timedelta(minutes=ctx.profile.journey(ctx.direction).buffer_min)
    legs, left = list(j.legs), list[int]()
    for i, x in enumerate(legs):
        if x.state not in MAY_HAVE_LEFT:
            continue
        cl = observe(pool, x)[0]
        if cl is None or cl.kind is LegKind.CANCELLED:
            continue
        dep = _departs(x, cl)
        if now >= dep:
            legs[i], left = replace(x, state=LegState.RIDING), [*left, i]
        elif now >= dep - buffer and x.state is LegState.PENDING:
            legs[i] = replace(x, state=LegState.BOARDING)
    return replace(j, legs=tuple(legs)), left


def next_good_instead_of(
    s: OccurrenceState, ctx: Ctx, pool: Pool, j: Journey, i: int
) -> tuple[bool, Connection | None]:
    """The next good connection in place of leg `i`, kept on the leg before it (7.7, rule 7).
    False: that change is not in NS data, so none can be judged."""
    x = j.legs[i]
    kept = observe(pool, j.legs[i - 1])[0] if i else None
    if i and kept is None:
        return False, None
    return True, find_next_good(s, ctx, pool, x.train, x.board_station, kept, i)


def left_note(s: OccurrenceState, ctx: Ctx, pool: Pool, j: Journey, i: int) -> AlertEvent:
    """A locked train departed with no missed-leg report: keep following it and show the next
    good connection in case the user is not on it (7.7, rule 7)."""
    x = j.legs[i]
    judged, c = next_good_instead_of(s, ctx, pool, j, i)
    text = f"your {_name(x)} train has left. If you are not on it, "
    if not judged:
        text += "check the NS app: the change from your current train is not in NS data."
    else:
        text += (
            f"next good connection: {hhmm(_departure(c))}."
            if c is not None
            else "no good connection found, check the NS app."
        )
    return make_event(ctx, EventKind.NEXT_CONNECTION, Severity.SILENT, text, x.train)


def _waited_at(s: OccurrenceState, station: str, dep: datetime) -> bool:
    """Q4: presence in `station`'s zone from BOARDING_LEAD before the expected departure until
    it, with no leave before the train (`on_location`): the user was there to board, so its
    leaving is no news, and a miss is told by later proof there (`_missed`). A first report
    only after the departure, or none, keeps the hedge (7.7)."""
    t = s.zone_reports.get(station)
    return t is not None and to_utc(dep) - BOARDING_LEAD <= to_utc(t) < to_utc(dep)


def _advance_all(
    s: OccurrenceState, ctx: Ctx, pool: Pool, live: bool
) -> tuple[OccurrenceState, list[AlertEvent]]:
    events: list[AlertEvent] = []
    lock = s.lock
    if live and lock is not None:
        lock, left = _advance(ctx, pool, lock)
        events = [
            left_note(s, ctx, pool, lock, i)
            for i in left
            if not _waited_at(
                s, (x := lock.legs[i]).board_station, _departs(x, present(observe(pool, x)[0]))
            )
        ]
    retained = tuple(_advance(ctx, pool, r)[0] for r in s.retained)
    # riding once leg 1 departs (7.7); a retained remainder exists only for a departed train
    riding = bool(retained) or (lock is not None and any(x.state in _DEPARTED for x in lock.legs))
    state = s.direction_state
    if riding and state in (DirectionState.IDLE, DirectionState.LOCKED):
        state = DirectionState.RIDING
    return replace(s, lock=lock, retained=retained, direction_state=state), events


def _missing_events(
    s: OccurrenceState, ctx: Ctx, pool: Pool, live: bool
) -> tuple[list[AlertEvent], dict[str, tuple[LegProgress, set[str], str]]]:
    """The watched legs not in the answers, and per watched train: a leg, its alighting stops and
    the prefix of its messages."""
    events: list[AlertEvent] = []
    seen: dict[str, tuple[LegProgress, set[str], str]] = {}
    for j in ((s.lock,) if live and s.lock else ()) + s.retained:
        on = "" if j is s.lock else on_prefix(j)
        for x in j.legs:
            if not _observed(x, ctx.now):
                continue
            if observe(pool, x)[1] is Observation.MISSING:
                text = f"{on}your {_name(x)} train is not in NS data."
                events.append(make_event(ctx, EventKind.MISSING, Severity.LOUD, text, x.train))
            seen.setdefault(train_key(x.train), (x, set(), on))[1].add(x.alight_stop)
    return events, seen


def _unclear_events(
    s: OccurrenceState,
    ctx: Ctx,
    pool: Pool,
    seen: Mapping[str, tuple[LegProgress, set[str], str]],
) -> tuple[OccurrenceState, list[AlertEvent]]:
    """Rule 23: on top of the merge, copies of one answer that disagree are shown once per
    values."""
    events: list[AlertEvent] = []
    for tk, (x, alights, on) in seen.items():
        u = disagreement(newest_copies(pool.answers, x.train, x.board_station), *sorted(alights))
        slot = unclear_slot(tk)
        if u is None or s.alert_keys.get(slot) == u.fingerprint:
            continue
        s = replace(s, alert_keys={**s.alert_keys, slot: u.fingerprint})
        text = on + data_unclear_text(u.label, u.station, u.copies)
        stated = AlertFacts(unclear=u.fingerprint)
        event = make_event(ctx, EventKind.DATA_UNCLEAR, Severity.LOUD, text, x.train, None, stated)
        date_ = ctx.schedule.key.service_date.isoformat()
        key = f"{EventKind.DATA_UNCLEAR.value}:{date_}:{tk}:{u.fingerprint}"
        events.append(replace(event, key=key))
    return s, events


def _follow_retained(
    s: OccurrenceState, ctx: Ctx, pool: Pool, asked: tuple[Query, ...]
) -> tuple[OccurrenceState, list[AlertEvent]]:
    """Rule 7: a retained remainder gets the locked-journey checks but is never relocked, so it
    keeps its observed progress and what the checks say, with the next good connection."""
    events: list[AlertEvent] = []
    retained = []
    for r in s.retained:
        first = r.legs[0].train
        on = on_prefix(r)
        alone = replace(s, lock=r, retained=(), cancellation_history=())
        t = _follow(alone, ctx, pool, asked, relock=False)
        retained.append(present(t.state.lock))
        for e in t.events:
            text = f"{display_name(e.profile_name)}: {on}{e.body}"
            tail = f":retained:{train_ident(first)}"
            key = e.key + tail
            f = alert_facts(e)
            facts = replace(f, on=on, moved_slot=f.moved_slot and f.moved_slot + tail)
            events.append(replace(e, text=text, key=key, facts=facts))
    return replace(s, retained=tuple(retained)), events


def on_poll(s: OccurrenceState, ctx: Ctx, poll: PollAnswers) -> Transition:
    """Apply one poll (7.4, rule 2). A result started under another generation is discarded
    (rule 21). One lock change per poll; the next poll, at the new generation, sees the rest.
    Ongoing facts (`missing`, `transfer_at_risk`, retained ones keyed `...:retained:<n>`) repeat
    every poll with a stable key: the news ledger (7.5) must drop repeats by key. Arrival
    acceptance (rule 6) and leg progress (7.7) run first; a lock whose journey ended (`DONE`)
    is no longer followed, while its retained remainders still are (rule 7). Any plan change
    raises the generation once (rule 21)."""
    if poll.started_generation != s.generation:
        return Transition(s, ())
    start = s
    s = replace(s, poll_records=(*s.poll_records, *poll.records)[-_KEEP_POLLS:])
    if (s.lock is None and not s.retained) or s.not_travelling or not poll.answers:
        return Transition(s, ())  # before a lock, auto_lock judges the usual
    pool = merged_pool(poll.answers)
    live = s.lock is not None and s.direction_state is not DirectionState.DONE
    s, events = _accept_all(s, ctx, pool, live)
    # Q3: before the advance, so the missed train gets no "has left" note; one move per poll
    home = _missed_from_home(s, ctx, pool) if live else None
    if home is not None:
        s, events = home.state, [*events, *home.events]
    s, more = _advance_all(s, ctx, pool, live)
    events += more
    missing, seen = _missing_events(s, ctx, pool, live)
    events += missing
    # Q9: skipped at the source, since it marks its slot as told when it emits
    s, unclear = (s, []) if quiet_watch(s, ctx) else _unclear_events(s, ctx, pool, seen)
    events += unclear
    asked = tuple(a.query for a in poll.answers)
    s, retained = _follow_retained(s, ctx, pool, asked)
    events += retained
    follow = live and home is None
    t = (_restore(s, ctx, pool) or _follow(s, ctx, pool, asked)) if follow else Transition(s, ())
    quiet = quiet_events(t.state, ctx, (*events, *t.events))
    return keep_stated(bumped(start, t.state), quiet)


# --- arrival acceptance, location, missed legs (7.2, 7.7, 7.12 rules 6, 7) ---

ERRAND_TIMEOUT = timedelta(minutes=45)
MISSED_GRACE = timedelta(
    minutes=3
)  # Q4, Q12 3b: a fix this soon after departure may be the train leaving
MISSED_CONFIRM = timedelta(minutes=12)  # Q12 rule 4: still there this long after it left is a miss
RIDING_SPEED = 8.0  # Q12 rule 2: m/s, faster than anyone walks or waits on a platform
ACTIVITY_FRESH = timedelta(minutes=5)  # Q12: an activity reported longer ago says nothing now
WIFI_LAG = timedelta(minutes=5)  # Q12 3a: a Wi-Fi tracker says "not home" up to this late
# iOS names (Stationary, Walking, Automotive), Android ones (still, on_foot, in_vehicle)
_RIDING_ACTIVITY = frozenset({"automotive", "in_vehicle"})
_AT_REST_ACTIVITY = frozenset({"stationary", "walking", "still", "on_foot"})
BOARDING_LEAD = timedelta(minutes=30)  # Q4: a still phone sends no repeat reports (location docs)
LAST_WATCH = timedelta(minutes=30)
_MISSABLE = (LegState.PENDING, LegState.BOARDING, LegState.RIDING)


def _accept(ctx: Ctx, x: LegProgress, pool: Pool, on: str) -> tuple[LegProgress, AlertEvent | None]:
    """Rule 6: accepted after two healthy answers at least 4 min apart, both at least 5 min after
    the reported arrival, show the same arrival; an accepted leg reopens when the arrival moves
    later or the train no longer reaches its alighting stop."""
    cl, _, seen = observe(pool, x)
    if x.arrival_phase is ArrivalPhase.INACTIVE or cl is None or seen is None:
        return x, None
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop), None)
    # passing, or cancelled without a reported time, is not served (classify.served)
    bad = cl.kind is LegKind.CANCELLED or stop is None or not served(stop)
    arr = None if stop is None else stop.arrival
    v = arr.value if arr is not None and arr.provenance is Provenance.REPORTED else None
    reset = replace(x, accept_value=None, accept_first_seen=None)
    if x.arrival_phase in (ArrivalPhase.ACCEPTED, ArrivalPhase.WATCHING):
        old = x.accept_value
        later = v is not None and old is not None and to_utc(v) > to_utc(old)
        if not (bad or later):
            return x, None
        reopened = replace(reset, state=LegState.RIDING, accepted_at=None, watch_until=None)
        if bad:
            what = (
                "is cancelled"
                if cl.kind is LegKind.CANCELLED
                else f"no longer reaches {x.alight_stop}"
            )
            kind = EventKind.CANCELLED if cl.kind is LegKind.CANCELLED else EventKind.CUT_SHORT
            text = f"{on}your {_name(x)} train {what}; following it again."
            return reopened, make_event(ctx, kind, Severity.LOUD, text, x.train)
        arrives = hhmm(present(v))
        text = f"{on}NS now shows your {_name(x)} train arriving at {arrives}; following it again."
        return reopened, make_event(ctx, EventKind.DELAYED, Severity.SILENT, text, x.train)
    if bad or v is None:
        return reset, None
    # the time of the answer that showed this arrival, never another query's (rule 6)
    if to_utc(seen) < to_utc(v) + ACCEPT_AFTER:
        return reset, None  # an estimate before its time: never acceptance
    first = x.accept_first_seen
    if x.arrival_phase is not ArrivalPhase.STABILIZING or to_utc(present(x.accept_value)) != to_utc(
        v
    ):
        return replace(x, accept_value=v, accept_first_seen=seen), None
    if to_utc(seen) - to_utc(present(first)) >= ACCEPT_APART:
        return x.accept_arrival(ctx.now, observed_at=seen), None
    return x, None


def _accept_journey(ctx: Ctx, j: Journey, pool: Pool, on: str) -> tuple[Journey, list[AlertEvent]]:
    pairs = [_accept(ctx, x, pool, on) for x in j.legs]
    legs = [x for x, _ in pairs]
    last = legs[-1]
    # every earlier watch ends with the last one (rule 6); a reopened last leg reopens them all
    w = None
    if last.state is LegState.DONE and last.accepted_at is not None:
        w = last.watch_until or (to_utc(last.accepted_at) + LAST_WATCH).astimezone(TZ)
    legs = [replace(x, watch_until=w if x.state is LegState.DONE else None) for x in legs]
    return replace(j, legs=tuple(legs)), [e for _, e in pairs if e is not None]


def _accept_all(
    s: OccurrenceState, ctx: Ctx, pool: Pool, live: bool
) -> tuple[OccurrenceState, list[AlertEvent]]:
    events: list[AlertEvent] = []
    lock = s.lock
    if live and lock is not None:
        lock, es = _accept_journey(ctx, lock, pool, "")
        events += es
    retained = []
    for r in s.retained:
        on = on_prefix(r)
        r, es = _accept_journey(ctx, r, pool, on)
        retained.append(r)
        events += es
    return replace(s, lock=lock, retained=tuple(retained)), events


def place_zone(p: Profile, station: str) -> str:
    """The configured zone of the home or work end; by its role name when none is configured.
    A station zone is named by its station code (8)."""
    role = "home" if station == p.home_station else "work"
    return (p.location.zones if p.location else {}).get(role, role)


def location_queries(s: OccurrenceState, ctx: Ctx, event: LocationEvent) -> list[Query]:
    """The poll's queries (rule 2) when a station event could show a locked leg missed (rule 7):
    an origin answer asked now can omit a train that already left."""
    lock, zone = s.lock, event.zone
    if event.kind == "leave" or lock is None:
        return []
    if not any(x.state in _MISSABLE and x.board_station == zone for x in lock.legs):
        return []
    return queries_needed(s, ctx)


def on_location(
    s: OccurrenceState,
    ctx: Ctx,
    event: LocationEvent,
    answer: ProviderAnswer | None = None,
    watched: Sequence[ProviderAnswer] = (),
) -> Transition:
    """Location only adds (7.2): leaving the origin zone sets a provisional lock, entering the
    first station zone a definitive one, station presence after a leg's expected departure that
    proves it (Q12) is a missed leg (rule 7). `answer`: a fresh healthy answer from the origin; without it only the
    travelling and station facts are kept, so the runtime passes the origin answer with every
    event. `watched`: the answers of `location_queries`, read only for a missed leg. Entering
    the origin station tells the train followed now, once (Q1). Presence at the origin place
    after the locked first train left is a miss from there once proven (Q3, Q12). A stale
    event counts as unknown. An `enter`, `report` or `still` is kept in `zone_reports`
    (presence).
    Nothing here ends or silences anything."""
    unchanged = Transition(s, ())
    if is_resolved(s) or event.zone is None:
        return unchanged
    if to_utc(ctx.now) - to_utc(event.at) > STALE_LOCATION:
        return unchanged
    start = s
    prev = s.zone_reports.get(event.zone)
    seen = (*((answer,) if answer is not None else ()), *watched)
    boards = _boarding_departure(s, event.zone, merged_pool(seen) if seen else None)
    at = to_utc(event.at)
    if prev is not None and at < to_utc(prev):
        return unchanged
    s = _note_evidence(s, event)
    # a leave places the user outside the zone: it is never presence (rule 6, 8.4). Q4: leaving
    # a boarding station before its train ends the wait; from the departure on, the user may be
    # aboard, so a later fix there keeps the earlier presence that says they waited.
    if event.kind == "leave":
        if prev is not None and (
            (boards is not None and at < boards)
            or event.zone == place_zone(ctx.profile, ctx.profile.origin(ctx.direction))
        ):
            s = replace(
                s, zone_reports={k: v for k, v in s.zone_reports.items() if k != event.zone}
            )
        if event.zone in s.station_seen:  # a dwell at an other station starts again
            s = replace(
                s, station_seen={k: v for k, v in s.station_seen.items() if k != event.zone}
            )
    elif prev is None or (at > to_utc(prev) and (boards is None or at < boards)):
        s = replace(s, zone_reports={**s.zone_reports, event.zone: event.at})
    t = _locate(s, ctx, event, answer, watched)
    # rule 21: a lock, catchability or missed-leg change raises the generation; a repeat
    # report that changes none of them keeps polls in flight
    return keep_stated(bumped(start, t.state), t.events)


def _activity(ev: Evidence, at: datetime, kinds: frozenset[str]) -> datetime | None:
    """When the phone reported an activity of `kinds`, if that is fresh at `at`, else None."""
    if ev.activity is None or ev.activity_at is None or ev.activity.casefold() not in kinds:
        return None
    t = to_utc(ev.activity_at)
    return t if at - t <= ACTIVITY_FRESH else None


def _note_evidence(s: OccurrenceState, event: LocationEvent) -> OccurrenceState:
    """Q12: keep the latest riding sign (a fix outside a zone, a fast fix, a fresh in-vehicle
    activity) and the time a place's Wi-Fi was seen connected. An enter with no sign of its own
    at or after the sign puts the user back in a zone: a GPS bounce over its edge, or the leave
    of a place overlapping the station the same fix enters, so the sign no longer says riding.
    A report or `still` does not: it can follow a fast fix with no new position."""
    ev, at = event.evidence, to_utc(event.at)
    signs = [at] if event.kind == "leave" or (ev.speed or 0.0) >= RIDING_SPEED else []
    if (riding := _activity(ev, at, _RIDING_ACTIVITY)) is not None:
        signs.append(riding)
    if signs and (s.moved_at is None or max(signs) > to_utc(s.moved_at)):
        s = replace(s, moved_at=max(signs))
    elif not signs and event.kind == "enter" and s.moved_at and to_utc(s.moved_at) <= at:
        s = replace(s, moved_at=None)
    if ev.wifi and event.kind != "leave" and event.zone is not None:
        s = replace(s, wifi_reports={**s.wifi_reports, event.zone: event.at})
    return s


def _miss_proven(
    s: OccurrenceState, seen: datetime, dep: datetime, event: LocationEvent | None = None
) -> bool:
    """Q12 rules 1-4: presence in the boarding zone or origin place, `seen` after the train left
    at `dep`, is only suspect: a phone can report a position minutes late. It proves a miss with
    no riding sign since the departure (rule 2) once it is MISSED_CONFIRM after it (rule 4), or
    when `event` carries a fresh at-rest activity reported after it, MISSED_GRACE on (rule 3b)."""
    if s.moved_at is not None and to_utc(s.moved_at) >= dep:
        return False
    if to_utc(seen) >= dep + MISSED_CONFIRM:
        return True
    if event is None or to_utc(event.at) < dep + MISSED_GRACE:
        return False
    rest = _activity(event.evidence, to_utc(event.at), _AT_REST_ACTIVITY)
    return rest is not None and rest >= dep


def _boarding_departure(s: OccurrenceState, zone: str, pool: Pool | None) -> datetime | None:
    """The latest known departure of the locked leg still to be boarded at this station."""
    lock = s.lock
    legs = () if lock is None else lock.legs
    x = next((x for x in legs if x.state in _MISSABLE and x.board_station == zone), None)
    if x is None:
        return None
    cl = observe(pool, x)[0] if pool is not None else None
    return _departs(x, cl) if cl is not None else to_utc(x.train.planned_departure)


def _locate(
    s: OccurrenceState,
    ctx: Ctx,
    event: LocationEvent,
    answer: ProviderAnswer | None,
    watched: Sequence[ProviderAnswer],
) -> Transition:
    p, d = ctx.profile, ctx.direction
    if s.direction_state is DirectionState.DONE:
        return Transition(s, ())  # only arrival evidence for a retained remainder (rule 7)
    if event.kind == "leave":
        if event.zone == place_zone(p, p.origin(d)):
            return _left_origin(s, ctx, event, answer)
        return Transition(s, ())
    seen = (*((answer,) if answer is not None else ()), *watched)  # `location_queries`
    missed = _missed(s, ctx, event, merged_pool(seen)) if seen else None
    if missed is not None:
        return missed
    if seen and event.zone == place_zone(p, p.origin(d)):
        home = _missed_from_home(s, ctx, merged_pool(seen), event)  # Q3: a home fix after departure
        if home is not None:
            return home
    if event.zone == p.origin(d) and s.prompt is not None and s.prompt.stage != "remember":
        s = replace(s, prompt=None)  # at the usual station after all: an older question is void
    if event.zone == p.origin(d) and s.boarding_station is not None:
        if s.lock is not None and s.lock.legs[0].state in _DEPARTED:
            return Transition(s, ())  # riding from the other station through the usual one
        # back at the usual station after all: a train from the other one is not taken
        s = replace(s, boarding_station=None)
        if s.lock is not None:
            s = replace(s, lock=None, direction_state=DirectionState.IDLE)
    if event.zone == boarding_origin(s, ctx):
        # one evidence pool: a newer watched answer's cancellation also rules a train out (rule 1)
        return _at_first_station(s, ctx, None if answer is None else merged_pool(seen))
    if other_station_candidate(s, ctx, event):
        return _at_other_station(s, ctx, event, answer)
    return Transition(s, ())


def other_station_candidate(s: OccurrenceState, ctx: Ctx, event: LocationEvent) -> bool:
    """A station event that may show the user boarding somewhere else: the trip is open and no
    train is followed yet (none, or TCC's own uncommitted guess not yet left). Also read by the
    runtime before it spends a request from that station."""
    p, d, zone = ctx.profile, ctx.direction, event.zone
    if p.location is None or zone is None or event.kind == "leave":
        return False
    places = {place_zone(p, p.home_station), place_zone(p, p.work_station)}
    if zone in places or zone in (boarding_origin(s, ctx), p.origin(d), p.destination(d)):
        return False
    if is_resolved(s) or s.direction_state is DirectionState.DONE:
        return False
    lock = s.lock
    if lock is None:
        return True
    if any(x.board_station == zone for x in lock.legs):
        return False  # a locked leg boards there: the missed-train rules decide (Q12)
    return (
        lock.source in _STATION_REPLACES
        and lock.commitment is not Commitment.COMMITTED
        and lock.legs[0].state in (LegState.PENDING, LegState.BOARDING)
        and not s.at_station
    )


def prompt_id(key: OccurrenceKey, station: str, stage: str, at: datetime) -> str:
    """10 lowercase hex: short enough for a button's callback, unique per question."""
    raw = f"{okey(key)}|{station}|{stage}|{to_utc(at).isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


ASK_CHOICES = (("yes", "Yes"), ("other", "Other train"), ("no", "No"))


REMEMBER_CHOICES = (("remember", "Yes"), ("forget", "No"))
PICK_MAX = 3
RIDE_GAP = timedelta(minutes=30)  # at another station this recently: a stop of a train ridden


def _prompt(
    s: OccurrenceState,
    ctx: Ctx,
    kind: EventKind,
    stage: PromptStage,
    text: str,
    choices: tuple[tuple[str, str], ...],
    name: str,
    station: str | None = None,
) -> Transition:
    """A new question about `station` (default the one `s` boards at), replacing any open one."""
    station = station or present(s.boarding_station)
    pid = prompt_id(s.key, station, stage, ctx.now)
    prompt = StationPrompt(pid, station, stage, None, choices, ctx.now, name)
    facts = AlertFacts(prompt_id=pid, choices=choices)
    e = make_event(ctx, kind, Severity.SILENT, text, None, pid, facts)
    return Transition(replace(s, prompt=prompt), (e,))


def remember_prompt(s: OccurrenceState, ctx: Ctx, name: str) -> Transition:
    """After Yes at a new station: offer to remember it. No answer remembers nothing."""
    text = f"remember {name} as a boarding station for your {ctx.direction.value} trips?"
    return _prompt(s, ctx, EventKind.STATION_REMEMBER, "remember", text, REMEMBER_CHOICES, name)


def pick_prompt(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, station: str, name: str
) -> Transition:
    """After Other train: up to PICK_MAX next good trains from `station`, as buttons. Nothing
    is confirmed yet, so an unanswered question changes nothing."""
    found: list[Connection] = []
    floor: datetime | None = None
    here = replace(s, at_station=True)
    for _ in range(PICK_MAX):
        c = find_next_good(here, ctx, answer, station=station, not_before=floor, earliest=True)
        if c is None:
            break
        found.append(c)
        floor = _departure(c) + timedelta(minutes=1)
    if not found:
        text = "no connection found, check the NS app."
        e = make_event(ctx, EventKind.NO_CONNECTION, Severity.SILENT, text)
        return Transition(replace(s, prompt=None), (e,))
    choices = tuple(
        (f"t{t:%H%M}", f"{t:%H:%M}")
        for t in (c.legs[0].train.planned_departure.astimezone(TZ) for c in found)
    )
    text = f"which train from {name}?"
    return _prompt(s, ctx, EventKind.STATION_ASK, "pick", text, choices, name, station)


def _at_other_station(
    s: OccurrenceState, ctx: Ctx, event: LocationEvent, answer: ProviderAnswer | None
) -> Transition:
    """The phone is at a station that is not where this direction starts. Driving past says
    nothing, and nor does a station minutes after another one: a train TCC does not follow stops
    at many. A known station (the profile's or a remembered one) with no train followed is
    followed by itself: the next train the user can catch there, told once. That lock stays
    provisional until a fix inside the station circle, so a walk past it is let go quietly. Any
    other station, or a known one while TCC holds its own guess, is asked about once per day,
    with Yes, Other train and No; no answer does nothing."""
    zone, at, ev = present(event.zone), to_utc(event.at), event.evidence
    if (ev.speed or 0.0) >= RIDING_SPEED or _activity(ev, at, _RIDING_ACTIVITY) is not None:
        return Transition(s, ())
    loc = present(ctx.profile.location)
    first = s.station_seen.get(zone)
    if first is None:
        s = replace(s, station_seen={**s.station_seen, zone: event.at})
        first = event.at
    if at - to_utc(first) < timedelta(minutes=loc.other_station_dwell_min):
        return Transition(s, ())
    places = {
        place_zone(ctx.profile, ctx.profile.home_station),
        place_zone(ctx.profile, ctx.profile.work_station),
    }
    if any(
        z != zone and z not in places and at - to_utc(t) < RIDE_GAP
        for z, t in s.zone_reports.items()
    ):
        return Transition(s, ())  # at another station minutes ago: a stop of a train ridden
    known = s.lock is None and (
        zone in ctx.profile.journey(ctx.direction).other_stations or zone in ctx.remembered
    )
    if not known and zone in s.asked_stations:
        return Transition(s, ())
    if answer is None or answer.query.origin != zone:
        return Transition(s, ())  # the next report asks NS again
    c = find_next_good(replace(s, at_station=True), ctx, answer, station=zone, earliest=True)
    if c is None:
        return Transition(s, ())
    first_leg, dest = c.legs[0], c.legs[-1].alight.name
    when, name = hhmm(first_leg.train.planned_departure), first_leg.board.name
    if known:
        # not at_station: the wide circle is no proof of boarding (`_never_boarded`)
        deadline = (at + ERRAND_TIMEOUT).astimezone(TZ)
        here = replace(s, boarding_station=zone, travelling=True, errand_deadline=deadline)
        new = journey_of(c, LockSource.LOCATION, Commitment.PROVISIONAL, answer.fetched_at)
        written = present(write_lock(here, new, expect=None, now=ctx.now))
        pool = merged_pool((answer,))
        return _with_status(written, ctx, pool, f"at {name}: following your ")
    pid = prompt_id(s.key, zone, "ask", ctx.now)
    prompt = StationPrompt(pid, zone, "ask", first_leg.train, ASK_CHOICES, ctx.now, name)
    asked = replace(s, prompt=prompt, asked_stations=s.asked_stations | {zone})
    text = f"are you taking the {when} from {name} to {dest}?"
    facts = AlertFacts(prompt_id=pid, choices=ASK_CHOICES)
    e = make_event(ctx, EventKind.STATION_ASK, Severity.SILENT, text, first_leg.train, pid, facts)
    return Transition(asked, (e,))


def _left_origin(
    s: OccurrenceState, ctx: Ctx, event: LocationEvent, answer: ProviderAnswer | None
) -> Transition:
    lock = s.lock
    if (
        lock is not None
        and lock.source is LockSource.AUTO
        and (
            _boarding_departure(
                s, lock.legs[0].board_station, merged_pool((answer,)) if answer else None
            )
            or to_utc(lock.legs[0].train.planned_departure)
        )
        <= to_utc(event.at)
    ):
        # a leave after the usual train left is often a late phone fix; the station decides
        return Transition(s, ())
    s = replace(s, travelling=True, tap_at=event.at)  # catchability: event time + buffer
    if answer is None or (lock is not None and lock.source is not LockSource.AUTO):
        return Transition(s, ())  # a chosen, station or later lock is kept
    c = find_next_good(s, ctx, answer)
    if c is None:
        return Transition(s, ())
    deadline = (to_utc(event.at) + ERRAND_TIMEOUT).astimezone(TZ)
    new = journey_of(c, LockSource.LOCATION, Commitment.PROVISIONAL, answer.fetched_at)
    # only an auto lock or none is replaced
    written = present(
        write_lock(replace(s, errand_deadline=deadline), new, expect=lock, now=ctx.now)
    )
    return Transition(written, ())


def _at_first_station(s: OccurrenceState, ctx: Ctx, pool: Pool | None) -> Transition:
    """Commit the lock; an auto or location lock is replaced by a station lock (7.2)."""
    lock = s.lock
    s = replace(s, at_station=True, travelling=True, errand_deadline=None)
    new = None if lock is None else replace(lock, commitment=Commitment.COMMITTED)
    if pool is not None and (lock is None or lock.source in _STATION_REPLACES):
        c = find_next_good(s, ctx, pool)
        if c is not None and (
            lock is None or [x.train for x in c.legs] != [x.train for x in lock.legs]
        ):
            written = present(
                write_lock(
                    s,
                    journey_of(c, LockSource.STATION, Commitment.COMMITTED, pool.fetched_at),
                    expect=lock,
                    now=ctx.now,
                )
            )
            return _with_status(written, ctx, pool)
        if c is not None and new is not None:  # the same train: only the source changes
            new = replace(new, source=LockSource.STATION)
    return _with_status(s if new is None else replace(s, lock=new), ctx, pool)


def _with_status(
    s: OccurrenceState, ctx: Ctx, pool: Pool | None, prefix: str = "at the station: your "
) -> Transition:
    """Q1 (2026-11-17): the first time the user is at the origin station, one silent status of
    the train followed now. None without a fresh answer (the next report tells it), without a
    lock, for a first train already left, or when NS shows it cancelled (6.3; its own alert
    speaks). `_STATUS_SLOT` keeps a GPS bounce or a report from repeating it, and the check-in
    (rule 20) is spent: the user already heard whether the train is on time."""
    lock, slot = s.lock, _STATUS_SLOT + (s.boarding_station or "")
    if pool is None or lock is None or slot in s.alert_keys:
        return Transition(s, ())
    x = lock.legs[0]
    cl = observe(pool, x)[0]
    trains = [y.train for y in lock.legs]
    c = next((c for c in pool.connections if [g.train for g in c.legs] == trains), None)
    if x.state not in (LegState.PENDING, LegState.BOARDING) or cl is None or c is None:
        return Transition(s, ())
    if cl.kind is LegKind.CANCELLED:
        return Transition(s, ())
    c = current_connection(evidence(pool), c)
    text = f"{prefix}{status_text(c.legs, cl.dep_delay_min)}."
    e = make_event(ctx, EventKind.AT_STATION, Severity.SILENT, text, x.train, "")
    told = replace(s, alert_keys={**s.alert_keys, slot: "1"}, all_good_evaluated=True)
    return Transition(told, (e,))


def _missed(s: OccurrenceState, ctx: Ctx, event: LocationEvent, pool: Pool) -> Transition | None:
    """Rule 7, Q2, Q12: presence at a leg's boarding station after its expected departure that
    proves a miss (`_miss_proven`). The leg is marked missed and dropped: the user cannot be on a
    train that left the station they stand at, so it is never retained and nothing more is said
    about it. Unproven, the user may be aboard with a phone that reports late: presence is kept,
    nothing moves, and a riding sign or a later vouch decides."""
    lock = s.lock
    if lock is None:
        return None
    legs = list(lock.legs)
    i = next(
        (i for i, x in enumerate(legs) if x.state in _MISSABLE and x.board_station == event.zone),
        None,
    )
    if i is None:
        return None
    x = legs[i]
    cl = observe(pool, x)[0]
    if cl is None or cl.kind is LegKind.CANCELLED:
        return None  # expected departure unknown, or a cancellation the follow step relocks
    dep = cl.leg.board.departure
    expected = dep.value if dep is not None and dep.value is not None else x.train.planned_departure
    if to_utc(ctx.now) <= to_utc(expected):
        return None  # not yet departed: the station step decides (7.2)
    first = i == 0
    s = replace(s, at_station=s.at_station or first, travelling=True, errand_deadline=None)
    if not _miss_proven(s, event.at, to_utc(expected), event):
        # suspect (Q12 rule 1): the user may be aboard; nothing moves
        return Transition(replace(s, lock=replace(lock, commitment=Commitment.COMMITTED)), ())
    legs[i] = replace(x, state=LegState.MISSED)
    s = replace(s, lock=replace(lock, legs=tuple(legs), commitment=Commitment.COMMITTED))
    if first:
        text = f"you missed the {hhmm(x.train.planned_departure)}"
        loud = Severity.SILENT if ctx.schedule.pick == "earliest_silent" else Severity.LOUD
        cause = Cause(EventKind.MISSED_FIRST_TRAIN, loud, text, x.train)
        kept = None
        # Q1: the miss is the station message, also when no connection follows
        s = replace(s, alert_keys={**s.alert_keys, _STATUS_SLOT: "1"}, all_good_evaluated=True)
    else:
        text = f"you missed your change at {x.board_station} to the {_name(x)} train"
        cause = Cause(EventKind.MISSED_TRANSFER, Severity.LOUD, text, x.train)
        kept = observe(pool, legs[i - 1])[0]
    asked = tuple(a.query for a in pool.answers)
    cut = Relock(
        legs=legs,
        first_replaced=i,
        station=x.board_station,
        kept=kept,
        cause=cause,
        source=LockSource.MISSED,
        at_station=first,
    )
    return _relock(s, ctx, pool, asked, cut)


def _missed_from_home(
    s: OccurrenceState, ctx: Ctx, pool: Pool, event: LocationEvent | None = None
) -> Transition | None:
    """Q3 (2026-11-17), rule 7: the locked first train has departed while the latest presence
    places the user in the origin place zone (home outbound, work on return): a fresh enter,
    report or `still` (7.2), no leave or station since (`travelling`, `at_station`), and too
    late to reach the train by the profile's own buffer from there (the catch rule of 7.2).
    Q12: that presence must prove the miss (`_miss_proven`), or the place's Wi-Fi, seen
    connected so late that even WIFI_LAG earlier the buffer could not be made (rule 3a). The
    leg is missed and the next train catchable from there is locked, with one message that says
    when to leave. A cancelled or unseen train is left to the follow step."""
    lock = s.lock
    if lock is None or s.travelling or s.at_station:
        return None
    x = lock.legs[0]
    p, d = ctx.profile, ctx.direction
    if x.state not in _MISSABLE or x.board_station != p.origin(d):
        return None  # the origin place cannot prove a miss at an other station (Q12)
    place = place_zone(p, p.origin(d))
    seen_at = s.zone_reports.get(place)
    now = to_utc(ctx.now)
    if seen_at is None or now - to_utc(seen_at) > STALE_LOCATION:
        return None
    cl = observe(pool, x)[0]
    if cl is None or cl.kind is LegKind.CANCELLED:
        return None
    expected = _departs(x, cl)
    buffer = timedelta(minutes=p.journey(d).buffer_min)
    if now < expected or to_utc(seen_at) + buffer <= expected:
        return None
    wifi = s.wifi_reports.get(place)
    by_wifi = (
        wifi is not None
        and buffer > WIFI_LAG  # rule 3a: within the lag the user may have just left
        and now - to_utc(wifi) <= STALE_LOCATION
        and to_utc(wifi) - WIFI_LAG + buffer > expected
        and (s.moved_at is None or to_utc(s.moved_at) < expected)
    )
    if not by_wifi and not _miss_proven(s, seen_at, expected, event):
        return None
    legs = (replace(x, state=LegState.MISSED), *lock.legs[1:])
    quiet = (p.alerts.quiet_repeat_misses and s.missed_from_origin) or (
        ctx.schedule.pick == "earliest_silent"
    )
    # a missed leg is never retained (Q2)
    s = replace(s, lock=replace(lock, legs=legs), missed_from_origin=True)
    text = f"you missed the {hhmm(x.train.planned_departure)}"
    severity = Severity.SILENT if quiet else Severity.LOUD
    cut = Relock(
        legs=legs,
        first_replaced=0,
        station=x.board_station,
        kept=None,
        cause=Cause(EventKind.MISSED_FIRST_TRAIN, severity, text, x.train),
        source=LockSource.MISSED,
        leave_by=True,
    )
    return _relock(s, ctx, pool, tuple(a.query for a in pool.answers), cut)


def origin_answer(answers: Sequence[ProviderAnswer], ctx: Ctx, s: OccurrenceState) -> Pool | None:
    """Every connection asked from the origin (an other boarding station once confirmed),
    newest answer first, so an earlier train searched for advice and the usual or locked train
    are judged together (7.6). Every answer of the poll is its evidence: each train is read as
    its current copy, so an older healthy copy never hides a newer cancellation, also one asked
    from a transfer station (6.2)."""
    origin = boarding_origin(s, ctx)
    mine = sorted(
        (a for a in answers if a.query.origin == origin),
        key=lambda a: to_utc(a.fetched_at),
        reverse=True,
    )
    if not mine:
        return None
    asked = next(a for a in answers if a.query.origin == origin).query  # the poll's own query
    conns = tuple(c for a in mine for c in a.connections)
    return Pool(asked, conns, mine[0].fetched_at, tuple(answers))

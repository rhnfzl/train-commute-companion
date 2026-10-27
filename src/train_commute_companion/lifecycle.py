"""Occurrence state, the lock writer, user actions, the follow step, location, arrival acceptance
and termination (7.2, 7.4, 7.7, 7.8, 7.12 rules 1, 2, 3, 6, 7, 8, 10, 11, 21, 7.13). Pure: `now`
always comes from the caller."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from train_commute_companion.board import (
    Catchability,
    GoodCheck,
    GoodKwargs,
    catch_from,
    choose_replacement,
    is_good,
    max_transfers,
)
from train_commute_companion.calendar import TZ, resolve_local
from train_commute_companion.classify import classify_leg, find_leg, served
from train_commute_companion.health import PollRecord
from train_commute_companion.location import LocationEvent
from train_commute_companion.match import (
    matches_pattern,
    remaining_pattern,
    route_changed,
    usual_connection,
)
from train_commute_companion.model import (
    AdviceRecord,
    AlertEvent,
    ClassifiedLeg,
    Connection,
    DatedSchedule,
    Direction,
    EventKind,
    Leg,
    LegKind,
    Observation,
    OccurrenceKey,
    Profile,
    Provenance,
    Severity,
    Signature,
    TrainId,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.transfer import check_transfer, transfer_minimum


class LockSource(StrEnum):
    AUTO = "auto"
    LOCATION = "location"
    STATION = "station"
    CHOOSE = "choose"
    MISSED = "missed"
    TIMEOUT = "timeout"
    RELOCK = "relock"
    RESTORE = "restore"


class Commitment(StrEnum):
    INTENDED = "intended"
    PROVISIONAL = "provisional"
    COMMITTED = "committed"


class LegState(StrEnum):
    PENDING = "pending"
    BOARDING = "boarding"
    RIDING = "riding"
    DONE = "done"
    MISSED = "missed"
    CANCELLED = "cancelled"
    RELOCKED = "relocked"


class DirectionState(StrEnum):
    IDLE = "idle"
    LOCKED = "locked"
    RIDING = "riding"
    DONE = "done"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class LegProgress:
    train: TrainId
    board_station: str
    intended_alight: str
    served_endpoint: str
    alight_stop: str
    alight_frozen: bool
    state: LegState
    accepted_at: datetime | None
    accept_value: datetime | None
    accept_first_seen: datetime | None
    watch_until: datetime | None
    # expected arrival + transfer minimum at alight_stop, last observed: the earliest onward
    # departure, so a broken change is searched from then on (7.4). Persisted state: a store
    # that drops it falls back to searching from the planned departure.
    onward_from: datetime | None = None


@dataclass(frozen=True, slots=True)
class Journey:
    legs: tuple[LegProgress, ...]
    source: LockSource
    commitment: Commitment
    planned_final_arrival: datetime


@dataclass(frozen=True, slots=True)
class OccurrenceState:
    key: OccurrenceKey
    generation: int
    route_revision: int
    settings_revision: int
    baseline_version: int | None
    direction_state: DirectionState
    lock: Journey | None
    retained: tuple[Journey, ...]
    travelling: bool
    tap_at: datetime | None
    at_station: bool
    errand_deadline: datetime | None
    not_travelling: bool
    cancellation_history: tuple[LegProgress, ...]  # trains replaced by a break, restorable (rule 8)
    missing_sent: bool
    all_good_evaluated: bool
    alert_keys: Mapping[str, str] = field(default_factory=dict)
    advice: AdviceRecord | None = None
    poll_records: tuple[PollRecord, ...] = ()
    # latest non-stale location time per zone: arrival evidence for a silent end (rule 6), and
    # what the news ledger reads to stop repeat silent messages. Persisted state: a store that
    # drops it turns a silent end after a restart into a loud stopped_following.
    zone_reports: Mapping[str, datetime] = field(default_factory=dict)
    # the baseline signature this occurrence started on (rule 10): a later re-seed of the
    # direction's history must not move a locked occurrence's schedule
    baseline_signature: Signature | None = None
    # True once `baseline_signature` is this occurrence's own (None then means it started with
    # no baseline); False only in stores written before it existed, hydrated at start
    baseline_frozen: bool = False


@dataclass(frozen=True, slots=True)
class Transition:
    state: OccurrenceState
    events: tuple[AlertEvent, ...]


@dataclass(frozen=True, slots=True)
class Ctx:
    profile: Profile
    direction: Direction
    schedule: DatedSchedule
    signature: Signature | None
    now: datetime


@dataclass(frozen=True, slots=True)
class Choose:
    service_date: date | None
    connection: Connection
    # the fresh healthy answers the connection was picked from (the latest board): identity
    # (6.1) and the change from a kept leg (rule 1) are judged on them, never on the copy alone
    answers: tuple[ProviderAnswer, ...]


@dataclass(frozen=True, slots=True)
class Leaving:
    service_date: date | None
    at: datetime


@dataclass(frozen=True, slots=True)
class NotTravelling:
    service_date: date | None
    whole_day: bool


UserAction = Choose | Leaving | NotTravelling

# a train the user may be on: departed and not done (7.12 rule 7). RIDING and MISSED say so by
# state; a PENDING or BOARDING leg says so once its planned departure has passed, since leg
# progress may lag the clock (a departed usual is auto-locked PENDING). A delayed train still at
# the platform also counts: retaining it errs on the safe side.
_MAY_BE_ON = (LegState.RIDING, LegState.MISSED)
_MAY_HAVE_LEFT = (LegState.PENDING, LegState.BOARDING)
_YIELDS_TO_CHOICE = (LockSource.AUTO, LockSource.STATION, LockSource.LOCATION)
_STATION_REPLACES = (LockSource.AUTO, LockSource.LOCATION)
_NEEDS_EXPECT_OVER_CHOICE = (LockSource.RELOCK, LockSource.RESTORE)
_UNRESOLVED_END = (DirectionState.DONE, DirectionState.EXPIRED)
_DEPARTED = (LegState.RIDING, LegState.DONE)  # by departure evidence: boarding progress (7.4)


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC)


def _resolved(s: OccurrenceState) -> bool:
    """Nothing left to follow (rule 6, consistent with `ends`): an occurrence whose main journey
    ended still runs while a retained remainder remains (rule 7)."""
    return s.not_travelling or (s.direction_state in _UNRESOLVED_END and not s.retained)


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
    )


def _bumped(old: OccurrenceState, new: OccurrenceState) -> OccurrenceState:
    """`new` with the generation raised once when its plan moved and nothing raised it yet."""
    if new.generation != old.generation or _plan(new) == _plan(old):
        return new
    return replace(new, generation=old.generation + 1)


def catch_from_state(s: OccurrenceState, buffer_min: int, now: datetime) -> Catchability:
    return catch_from(now, buffer_min, s.tap_at if s.travelling else None, s.at_station)


def progress_of(leg: Leg) -> LegProgress:
    a = leg.alight.code
    return LegProgress(
        leg.train, leg.board.code, a, a, a, False, LegState.PENDING, None, None, None, None
    )


def journey_of(c: Connection, source: LockSource, commitment: Commitment) -> Journey:
    last = c.legs[-1]
    arrival = last.alight.planned_arrival or last.train.planned_departure
    return Journey(tuple(progress_of(x) for x in c.legs), source, commitment, arrival)


def _unfinished(j: Journey) -> LegProgress | None:
    return next((x for x in j.legs if x.state is not LegState.DONE), None)


def _may_be_on(x: LegProgress, now: datetime | None) -> bool:
    if x.state in _MAY_BE_ON:
        return True
    left = now is not None and x.train.planned_departure.astimezone(UTC) <= now.astimezone(UTC)
    return left and x.state in _MAY_HAVE_LEFT


def _rides(j: Journey | None, train: TrainId, now: datetime | None) -> bool:
    """The user may be on `train` as the first unfinished leg of `j`."""
    first = None if j is None else _unfinished(j)
    return first is not None and first.train == train and _may_be_on(first, now)


def write_lock(
    s: OccurrenceState, new: Journey, *, expect: Journey | None, now: datetime | None
) -> OccurrenceState | None:
    """Compare-and-set lock write. None when refused (stale `expect` or precedence).
    `now` decides whether a replaced train has departed, so it is retained (7.12 rule 7); None
    judges by leg state only, for a caller that already knows the replaced train is not ridden."""
    old = s.lock
    if expect is not None and old != expect:
        return None
    if old is not None and old.source is LockSource.CHOOSE:
        if new.source in _YIELDS_TO_CHOICE:
            return None
        if new.source in _NEEDS_EXPECT_OVER_CHOICE and expect is None:
            return None
    # completed travel stays; a caller passing a done leg again may update its watched fields
    # (served endpoint, alighting stop, acceptance). A choice brings its own prefix (7.12 r7):
    # completed legs of another journey are never merged into it.
    newer = {x.train: x for x in new.legs}
    done = (
        ()
        if old is None or new.source is LockSource.CHOOSE
        else tuple(newer.get(x.train, x) for x in old.legs if x.state is LegState.DONE)
    )
    done_trains = {x.train for x in done}
    legs = done + tuple(x for x in new.legs if x.train not in done_trains)
    retained = s.retained
    if old is not None:
        kept = {x.train for x in legs}
        replaced = [
            i
            for i, x in enumerate(old.legs)
            if x.state is not LegState.DONE and x.train not in kept
        ]
        # from the first replaced train the user may be on: an earlier replaced one (say a
        # cancelled train never boarded) does not hide a later one that may be ridden
        on = next((i for i in replaced if _may_be_on(old.legs[i], now)), None)
        if on is not None:
            retained += (replace(old, legs=old.legs[on:]),)
    state = s.direction_state
    return replace(
        s,
        lock=replace(new, legs=legs),
        retained=retained,
        generation=s.generation + 1,
        direction_state=DirectionState.LOCKED if state is DirectionState.IDLE else state,
        errand_deadline=s.errand_deadline if new.commitment is Commitment.PROVISIONAL else None,
    )


def _hhmm(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H:%M")


DELAY_STEP_MIN = 3
ON_TIME_MAX_MIN = 1


def announced(last: int | None, now: int | None) -> int | None:
    """The delay the user was last told, moved only by news (7.5): a 3-min step either way
    from what was said, or back to normal at 1 min or less. A wobble around the last stated
    delay keeps it, so +5 to +6 or +3 to +2 says nothing. No reported time keeps it too."""
    if now is None:
        return last
    said = last or 0
    if said > ON_TIME_MAX_MIN and now <= ON_TIME_MAX_MIN:
        return 0
    return now if abs(now - said) >= DELAY_STEP_MIN else last


# a live delay in a key, before `_stated` announces it: `|~N`, or tagged `|d~N` / `|a~N`
# (departure, arrival); `?` is a delay NS did not report, which keeps the last stated one
_STATED = re.compile(r"\|(\w?)~(-?\d+|\?)")
# a platform in a key: `track N`, with `|planned` when N is the planned platform again
_TRACK = re.compile(r"track [^|:]+(\|planned)?")


def _recovery(e: AlertEvent, before: Sequence[int], said: Sequence[int]) -> AlertEvent | None:
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
    head = e.text.partition(" train ")[0]
    key = kind.value + e.key[len(e.kind.value) :]
    return replace(e, kind=kind, severity=Severity.SILENT, text=f"{head} train {tail}", key=key)


def _stated(s: OccurrenceState, events: Sequence[AlertEvent]) -> Transition:
    """A stated replacement time or delay in a key moves only by news (7.5): the delay last
    stated per fact is kept in `alert_keys`, so 09:05 to 09:08 is news again while 09:05 to
    09:06 is not. A delay brought back to on time becomes its recovery message."""
    keys = dict(s.alert_keys)
    out = []
    for e in events:
        if e.kind is EventKind.PLATFORM_CHANGE:
            # per boarding segment: a train back on its planned platform is news only once a
            # change was announced for it (1 -> 5 -> 1 says so, an unchanged 1 says nothing)
            slot = "moved#" + _TRACK.sub("track", e.key)
            if (m := _TRACK.search(e.key)) is not None and m[1]:
                if slot not in keys:
                    continue
            else:
                keys[slot] = "1"
            out.append(e)
            continue
        ms = list(_STATED.finditer(e.key))
        if not ms:
            out.append(e)
            continue
        base = _STATED.sub("", e.key)
        slots = [base + (f"#{m[1]}" if m[1] else "") for m in ms]
        was = [None if keys.get(x) is None else int(keys[x]) for x in slots]
        live = [None if m[2] == "?" else int(m[2]) for m in ms]
        told = [announced(w, n) for w, n in zip(was, live, strict=True)]
        if told != was:  # news: the message states every live value, so each is remembered
            told = [t if n is None else n for t, n in zip(told, live, strict=True)]
        said = [t or 0 for t in told]
        before = [w or 0 for w in was]
        keys.update({x: str(t) for x, t in zip(slots, said, strict=True)})
        e = replace(e, key=f"{base}|stated+{','.join(map(str, said))}")
        ev = _recovery(e, before, said) if e.kind is EventKind.DELAYED else e
        if ev is not None:
            out.append(ev)
    return Transition(replace(s, alert_keys=keys) if keys != s.alert_keys else s, tuple(out))


def _event(
    ctx: Ctx,
    kind: EventKind,
    severity: Severity,
    text: str,
    train: TrainId | None = None,
    facts: str | None = None,
) -> AlertEvent:
    """The key carries the facts the message states (7.5): `facts` when given, else the text,
    so a changed endpoint, replacement or revised arrival is news while a repeat is not.
    Pass `facts` when the text holds a live time that drifts without being news."""
    key = ctx.schedule.key
    name = ctx.profile.name.replace("_", " ").title()
    ident = "" if train is None else f":{train.train_number}@{train.planned_departure.isoformat()}"
    return AlertEvent(
        ctx.profile.id,
        ctx.profile.name,
        ctx.direction,
        key.service_date,
        None,
        kind,
        severity,
        train,
        f"{name}: {text}",
        f"{kind.value}:{key.service_date.isoformat()}{ident}:{text if facts is None else facts}",
        ctx.now,
    )


def _next_good(
    s: OccurrenceState,
    ctx: Ctx,
    answer: ProviderAnswer,
    after: TrainId | None = None,
    station: str | None = None,
    kept: ClassifiedLeg | None = None,
    n_before: int = 0,
) -> Connection | None:
    """The next good connection from `station` (default the origin), not starting on `after`.
    `after` only leaves the ranking: identity is judged on the whole answer (6.1)."""
    at = station or ctx.profile.origin(ctx.direction)
    c, _ = choose_replacement(
        answer,
        usual_remaining=remaining_pattern(ctx.profile.journey(ctx.direction).transfers, at),
        candidates=[c for c in answer.connections if after is None or c.legs[0].train != after],
        **_good_kw(s, ctx, at, kept, n_before),
    )
    return c


def _departure(c: Connection) -> datetime:
    d = c.legs[0].board.departure
    t = d.value if d and d.value else c.legs[0].train.planned_departure
    return t.astimezone(UTC)


def auto_lock(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer) -> Transition:
    """Lock the usual connection at or after `auto_lock_at` unless a lock exists (7.2, 6.3).
    A usual connection that is already broken is still locked: the follow step relocks it and
    sends the cancellation (7.4)."""
    unchanged = Transition(s, ())
    now = ctx.now.astimezone(UTC)
    if s.lock is not None or s.not_travelling or now < ctx.schedule.auto_lock_at.astimezone(UTC):
        return unchanged
    p, d = ctx.profile, ctx.direction
    q = answer.query
    # provider failures raise, so any answer is healthy; one asked from elsewhere or after the
    # usual departure cannot prove the usual is absent, so it is not judged (6.3, rule 2)
    if (q.origin, q.destination) != (p.origin(d), p.destination(d)) or q.at.astimezone(
        UTC
    ) > ctx.schedule.first_departure.astimezone(UTC):
        return unchanged
    usual = usual_connection(
        answer, p.origin(d), ctx.schedule.first_departure, p.journey(d).transfers, ctx.signature
    )
    events: list[AlertEvent] = []
    if usual is not None:
        locked = write_lock(
            s, journey_of(usual, LockSource.AUTO, Commitment.INTENDED), expect=None, now=now
        )
        assert locked is not None  # no lock existed
        first = usual.legs[0]
        if _departure(usual) < now and classify_leg(first, first.alight.code).kind in _RUNS:
            # the user may be on it; show where to go if not (7.12 rule 7)
            nxt = _next_good(s, ctx, answer, after=first.train)
            text = f"your usual {_hhmm(ctx.schedule.first_departure)} has left. " + (
                f"If you are not on it, next good connection: {_hhmm(_departure(nxt))}."
                if nxt is not None
                else "If you are not on it, no good connection found, check the NS app."
            )
            events.append(
                _event(ctx, EventKind.NEXT_CONNECTION, Severity.SILENT, text, first.train)
            )
        # 7.2: a usual already broken now is relocked at once, with its warning, from this
        # answer; departure progress comes first, so a train the user may already be on is
        # warned about, never relocked away (7.4, rule 7). The first train has its note above.
        pool = _pool((answer,))
        locked, left = _advance_all(locked, ctx, pool, live=True)
        events += [e for e in left if e.train != first.train]
        t = _follow(locked, ctx, pool, (answer.query,))
        return _stated(t.state, (*events, *t.events))
    if not s.missing_sent:
        events.append(
            _event(
                ctx,
                EventKind.MISSING,
                Severity.LOUD,
                f"your usual {_hhmm(ctx.schedule.first_departure)} is not in NS data.",
            )
        )
        s = replace(s, missing_sent=True)
    nxt = _next_good(s, ctx, answer)
    if nxt is None:
        text = "no connection found, check the NS app."
        events.append(_event(ctx, EventKind.NO_CONNECTION, Severity.LOUD, text))
        return Transition(s, tuple(events))
    locked = write_lock(
        s, journey_of(nxt, LockSource.AUTO, Commitment.INTENDED), expect=None, now=now
    )
    assert locked is not None
    return Transition(locked, tuple(events))


def _ridden_source(s: OccurrenceState, train: TrainId, now: datetime | None) -> Journey | None:
    """The lock or retained remainder whose first unfinished leg is `train`, a train the user
    may be on. A retained remainder holds such a train by construction, whatever its state."""
    if _rides(s.lock, train, now):
        return s.lock
    return next((j for j in s.retained if (f := _unfinished(j)) and f.train == train), None)


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
                and (isinstance(action, NotTravelling) or not _resolved(x))
            ),
            None,
        )  # fmt: skip
    open_ = sorted((x for x in states if not _resolved(x)), key=lambda x: x.key.service_date)
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
    return Transition(s, (_event(ctx, EventKind.CHOOSE_REFUSED, Severity.LOUD, text),))


def _choose(s: OccurrenceState, ctx: Ctx, action: Choose) -> Transition:
    p, d = ctx.profile, ctx.direction
    j = p.journey(d)
    c = action.connection
    if not action.answers:
        return _refuse(s, ctx, "no fresh NS answer to check it against")
    pool = _pool(action.answers)
    source = _ridden_source(s, c.legs[0].train, ctx.now)
    # the kept prefix comes from the journey the choice continues, never from another one
    base = s.lock if source is None else source
    done = () if base is None else base.legs[: _first_open(base)]
    # a missed-transfer remainder starts at the transfer: the lock legs into it are its past
    head: tuple[LegProgress, ...] = ()
    if source is not None and source is not s.lock and s.lock is not None:
        at = source.legs[0].board_station
        ends = [i for i, x in enumerate(s.lock.legs) if x.alight_stop == at]
        head = s.lock.legs[: ends[0] + 1] if ends else ()
    if source is not None and len(c.legs) == 1:
        check = GoodCheck(c.destination == p.destination(d), "does not arrive at the destination")
        if check.ok and len(head) + len(done) > max_transfers(j.transfers):
            check = GoodCheck(False, "over the transfer cap")
    else:
        # a train the user may be on is kept: catchability applies only to the legs after it,
        # and the change from it (or from the last done leg) is checked like any other (rule 1)
        rest = c if source is None else Connection(c.legs[1:], c.status, c.ctx_recon)
        if source is not None:
            ridden = replace(source.legs[len(done)], intended_alight=rest.origin)
            kept, station = _observe(pool, ridden)[0], rest.origin
            n_before = len(head) + len(done) + 1
        else:
            kept = _observe(pool, done[-1])[0] if done else None
            n_before = len(done)
            station = done[-1].alight_stop if done else p.origin(d)
        if n_before and kept is None:
            return _refuse(s, ctx, "the change from your current train is not in NS data")
        check = is_good(rest, pool, **_good_kw(s, ctx, station, kept, n_before))
    if not check.ok:
        return _refuse(s, ctx, check.reason)
    riding = source is not None or s.travelling
    new = journey_of(c, LockSource.CHOOSE, Commitment.COMMITTED if riding else Commitment.INTENDED)
    legs = new.legs
    if source is not None:  # the ridden prefix keeps its progress, to the chosen stop
        have = {x.train: x for x in source.legs}

        def reuse(h: LegProgress, stop: str) -> LegProgress:
            if h.intended_alight == stop:
                return h
            return replace(_move_stop(h, stop, stop), intended_alight=stop)

        legs = tuple(
            progress_of(y) if (h := have.get(y.train)) is None else reuse(h, y.alight.code)
            for y in c.legs
        )
    new = replace(new, legs=(*head, *done, *legs))
    # a choice ends every retained remainder; the writer retains the old lock if ridden
    written = write_lock(replace(s, retained=()), new, expect=s.lock, now=ctx.now)
    assert written is not None  # a choice always writes over the lock it read
    if source is not None:  # a ridden journey, also one promoted after the main one ended
        written = replace(written, direction_state=DirectionState.RIDING)
    return Transition(written, ())


def apply_action(s: OccurrenceState, ctx: Ctx, action: UserAction) -> Transition:
    if isinstance(action, Choose):
        return _choose(s, ctx, action)
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
            direction_state=DirectionState.EXPIRED,
            retained=(),
            generation=gen,
        ),
        (),
    )


# --- follow, relock, restore (7.4, 7.12 rules 2, 3, 8, 21) ---

_BAD = (LegKind.CANCELLED, LegKind.CUT_SHORT)
_RUNS = (LegKind.OK, LegKind.DELAYED)
_AT_RISK_LOUD = timedelta(minutes=30)
_PLATFORM_LOUD = timedelta(minutes=15)
_KEEP_POLLS = 20  # shortcut: health reads only the latest records; keep more if it ever needs a day


@dataclass(frozen=True, slots=True)
class Board:
    answers: tuple[ProviderAnswer, ...]  # healthy answers only: a failed request raised
    polls: tuple[PollRecord, ...]
    started_generation: int


@dataclass(frozen=True, slots=True)
class _Pool(ProviderAnswer):
    """All answers of one poll merged for ranking, plus each answer apart: per-leg evidence
    keeps the answer and the time it was observed in (rule 6, rule 20)."""

    answers: tuple[ProviderAnswer, ...] = ()


def _first_open(j: Journey) -> int:
    return next((i for i, x in enumerate(j.legs) if x.state is not LegState.DONE), len(j.legs))


def _observed(x: LegProgress, now: datetime) -> bool:
    """Unfinished, or done and still watched; an earlier leg's watch ends with the last (rule 6)."""
    if x.state in (LegState.CANCELLED, LegState.RELOCKED):
        return False
    w = x.watch_until
    return x.state is not LegState.DONE or w is None or now.astimezone(UTC) < w.astimezone(UTC)


def _restorable(s: OccurrenceState, ctx: Ctx, h: LegProgress) -> bool:
    """Not yet departed minus the catchability margin (rule 2): the buffer at the origin, else
    none (the user is on the way and the kept leg's transfer check decides)."""
    if h.board_station == ctx.profile.origin(ctx.direction):
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


def _since(fresh: datetime | None, asked: datetime | None) -> datetime | None:
    """Only an answer asked from the station at or after this proves there is no connection;
    the time the last poll asked for counts, so a drifting delay does not keep it quiet."""
    if fresh is None or asked is None:
        return fresh
    return min(fresh, asked, key=_utc)


def queries_needed(s: OccurrenceState, ctx: Ctx) -> list[Query]:
    """Rule 2. Each query carries a full date-time, so a leg after midnight is asked on its own
    date (7.13). The first query is from the first leg not done, else the usual (6.3); every
    retained remainder is asked for also without a lock (rule 7)."""
    p, d = ctx.profile, ctx.direction
    dest = p.destination(d)
    out: list[Query] = []
    seen: set[tuple[str, datetime]] = set()

    def add(station: str, at: datetime) -> None:
        key = (station, _utc(at))  # local equality would merge the two 02:30s of the DST fold
        if key not in seen:
            seen.add(key)
            out.append(Query(station, dest, at))

    if s.lock is None:  # the usual at its effective departure, also when past (6.3)
        add(p.origin(d), ctx.schedule.first_departure)
    else:
        k = _first_open(s.lock)
        if k < len(s.lock.legs):
            add(s.lock.legs[k].board_station, s.lock.legs[k].train.planned_departure)
    for j in (*((s.lock,) if s.lock else ()), *s.retained):
        k = _first_open(j)
        for i, x in enumerate(j.legs):
            if _observed(x, ctx.now) or i == k - 1:  # k - 1: the next transfer's incoming leg
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


def _pool(answers: Sequence[ProviderAnswer]) -> _Pool:
    """All answers plus every connection's suffixes, so a search from a transfer station can use
    an answer asked from earlier on the route (rule 3)."""
    conns = [c for a in answers for c in a.connections]
    frags = [Connection(c.legs[i:], c.status, None) for c in conns for i in range(1, len(c.legs))]
    newest = max(answers, key=lambda a: _utc(a.fetched_at))
    return _Pool(answers[0].query, (*conns, *frags), newest.fetched_at, tuple(answers))


def _observe(
    pool: _Pool, x: LegProgress
) -> tuple[ClassifiedLeg | None, Observation, datetime | None]:
    """The leg's classification, its observation over the whole poll, and when the answer it
    was read from was fetched."""
    leg, ob = find_leg(pool, x.train, x.board_station)
    if leg is None:
        return None, ob, None
    newest_first = sorted(pool.answers, key=lambda a: _utc(a.fetched_at), reverse=True)
    runs = [
        (a.fetched_at, classify_leg(g, x.intended_alight))
        for a in newest_first
        for c in a.connections
        for g in c.legs
        if g.train == leg.train and g.board.code == x.board_station
    ]
    # by instant: two local 02:30s of the DST fold are an hour apart (7.13)
    newest = [r for r in runs if _utc(r[0]) == _utc(runs[0][0])]

    def far(r: tuple[datetime, ClassifiedLeg]) -> tuple[bool, int]:
        return r[1].kind not in _BAD, len(r[1].leg.stops)

    # one train shows in several connections, each cut where it changes: take the farthest
    # run, unless the newest answer shows it cancelled at its boarding stop, which no older
    # running copy can overrule
    seen, best = max(newest, key=far)
    if best.kind is not LegKind.CANCELLED:
        seen, best = max(runs, key=far)
    return replace(best, observation=ob), ob, seen


def _name(x: LegProgress) -> str:
    return f"{_hhmm(x.train.planned_departure)} {x.board_station} to {x.intended_alight}"


def _good_kw(
    s: OccurrenceState, ctx: Ctx, station: str, kept: ClassifiedLeg | None, n_before: int
) -> GoodKwargs:
    """`n_before` legs stay before the new suffix; the last of them is `kept`, which every
    caller must have observed when there is one (rule 1)."""
    assert n_before == 0 or kept is not None, "a kept leg's change must be checked"
    p, d = ctx.profile, ctx.direction
    j = p.journey(d)
    catch = (
        catch_from_state(s, j.buffer_min, ctx.now)
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
    """Observed progress only; the generation is raised once per poll by `_bumped`."""
    assert s.lock is not None
    return replace(s, lock=replace(s.lock, legs=tuple(legs)))


def _write(s: OccurrenceState, new: Journey) -> OccurrenceState:
    # None: every caller has checked that the replaced train is not ridden
    written = write_lock(s, new, expect=s.lock, now=None)
    assert written is not None  # relock and restore pass the lock just read as `expect`
    return written


def _move_stop(x: LegProgress, end: str, stop: str) -> LegProgress:
    """A new served endpoint and alighting stop; an accepted arrival at another stop is no
    longer the arrival, so the leg is followed again (rule 6)."""
    moved = replace(x, served_endpoint=end, alight_stop=stop)
    if stop == x.alight_stop or x.state is not LegState.DONE:
        return moved
    return replace(
        moved,
        state=LegState.RIDING,
        accepted_at=None,
        accept_value=None,
        accept_first_seen=None,
        watch_until=None,
    )


def _restore(s: OccurrenceState, ctx: Ctx, pool: _Pool) -> Transition | None:
    """Rule 8: a reinstated train moves back only for legs not boarded and only if good."""
    lock = s.lock
    assert lock is not None
    p, d = ctx.profile, ctx.direction
    legs = lock.legs
    for h in s.cancellation_history:
        if not _restorable(s, ctx, h):
            continue
        for j in range(len(legs)):
            station = p.origin(d) if j == 0 else legs[j - 1].intended_alight
            rest = legs[j:]
            if station != h.board_station or rest[0].train == h.train:
                continue
            if any(y.state is LegState.DONE or _may_be_on(y, ctx.now) for y in rest):
                continue
            kept = _observe(pool, legs[j - 1])[0] if j else None
            if j and kept is None:
                continue
            kw = _good_kw(s, ctx, station, kept, j)
            good = [
                c
                for c in pool.connections
                if c.legs[0].train == h.train and is_good(c, pool, **kw).ok
            ]
            if not good:
                continue
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
                (_event(ctx, EventKind.RESTORED, Severity.SILENT, text, h.train),),
            )
    return None


Cause = tuple[EventKind, Severity, str, TrainId]


def _relock(
    s: OccurrenceState,
    ctx: Ctx,
    pool: _Pool,
    asked: tuple[Query, ...],
    legs: Sequence[LegProgress],
    j: int,
    station: str,
    kept: ClassifiedLeg | None,
    cause: Cause,
    history: LegProgress | None = None,
    over_cap: Connection | None = None,
    since: datetime | None = None,
    source: LockSource = LockSource.RELOCK,
) -> Transition:
    """Replace legs[j:] by the next good connection from `station` (7.4). `over_cap` is the
    actual route of the boarded train legs[j - 1], followed when nothing good fits (6.3).
    `since`: only an answer asked from `station` at or after it proves there is no connection.
    A kept leg legs[j - 1] that is not in the answers cannot have its change checked (rule 1):
    the cause is still reported, and nothing is relocked on it."""
    lock = s.lock
    assert lock is not None
    p, d = ctx.profile, ctx.direction
    kind, sev, text, train = cause
    if j and kept is None:
        note = f"{text}; the change from your current train is not in NS data, check the NS app."
        return Transition(_progress(s, legs), (_event(ctx, kind, sev, note, train),))
    usual = remaining_pattern(p.journey(d).transfers, station)
    # an older answer's running copy of the train being replaced is no replacement for it
    cands = [c for c in pool.connections if history is None or c.legs[0].train != history.train]
    c, how = choose_replacement(
        pool, usual_remaining=usual, candidates=cands, **_good_kw(s, ctx, station, kept, j)
    )
    if c is None and over_cap is not None:
        end = over_cap.legs[0].alight.code
        ride = _move_stop(legs[j - 1], end, end)
        new = _journey(lock, (*legs[: j - 1], ride), over_cap.legs[1:], LockSource.RELOCK)
        over = j - 1 + len(over_cap.legs) - 1 > max_transfers(p.journey(d).transfers)
        via = ", ".join(over_cap.transfer_stations) or end
        note = f"no good connection within your usual changes; following NS via {via}" + (
            ", the extra legs are beyond the sensors." if over else "."
        )
        reroute = _event(ctx, EventKind.ROUTE_CHANGED, Severity.LOUD, f"{text}: {note}", train)
        if kind is EventKind.ROUTE_CHANGED:
            return Transition(_write(s, new), (reroute,))
        return Transition(_write(s, new), (_event(ctx, kind, sev, f"{text}.", train), reroute))
    if c is None:
        events = [_event(ctx, kind, sev, f"{text}.", train)]
        if any(q.origin == station and (since is None or _utc(q.at) >= _utc(since)) for q in asked):
            nothing = "no connection found, check the NS app."
            events.append(_event(ctx, EventKind.NO_CONNECTION, Severity.LOUD, nothing))
        return Transition(_progress(s, legs), tuple(events))
    via = ", ".join(c.transfer_stations) or "a direct train"
    # the replacement by identity, not by its live time: a drifting estimate is not news (7.5)
    # and its stated time by its delay, which `_stated` turns into 3-min news steps
    late = (_departure(c) - _utc(c.legs[0].train.planned_departure)) // timedelta(minutes=1)
    facts = (
        f"{text}|"
        + ",".join(
            f"{x.train.train_number}@{x.train.planned_departure.isoformat()}" for x in c.legs
        )
        + f"|~{late}"
    )
    text += f". Next good connection: {_hhmm(_departure(c))}" + (
        f", new route via {via}." if how == "new_route" else "."
    )
    written = _write(s, _journey(lock, legs[:j], c.legs, source))
    if history is not None and history.train not in {y.train for y in s.cancellation_history}:
        written = replace(written, cancellation_history=(*s.cancellation_history, history))
    return Transition(written, (_event(ctx, kind, sev, text, train, facts),))


def _onward(
    p: Profile, x: LegProgress, cl: ClassifiedLeg, ncl: ClassifiedLeg | None
) -> datetime | None:
    """Expected arrival at the alighting stop plus the transfer minimum: the earliest onward
    departure, for every kind of break (7.4). UTC arithmetic keeps the DST fold (7.13)."""
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop and served(st)), None)
    arr = None if stop is None or stop.arrival is None else stop.arrival
    t = None if arr is None else arr.value or arr.planned
    if t is None:
        return None
    msg = None if ncl is None else ncl.leg.transfer_message_min
    minimum = transfer_minimum(x.alight_stop, p.transfer_min, cl.leg.transfer_time_to_next, msg)
    return (_utc(t) + timedelta(minutes=minimum)).astimezone(TZ)


def _at_risk(ctx: Ctx, x: LegProgress, nxt: LegProgress, reason: str) -> AlertEvent:
    soon = _utc(nxt.train.planned_departure) - _utc(ctx.now) <= _AT_RISK_LOUD
    text = f"your change at {x.alight_stop} is at risk: {reason}."
    sev = Severity.LOUD if soon else Severity.SILENT
    return _event(ctx, EventKind.TRANSFER_AT_RISK, sev, text, nxt.train)


def _limit_at(limit: time, planned: datetime) -> datetime:
    """`latest_acceptable_arrival` as an instant: the one nearest the planned arrival, so a
    23:55 limit for a 23:50 arrival stays on that evening when the train slips past midnight."""
    day = planned.astimezone(TZ).date()
    options = [resolve_local(day + timedelta(days=n), limit, edge="start") for n in (-1, 0, 1)]
    return min(options, key=lambda t: abs(_utc(t) - _utc(planned)))


def _delayed(ctx: Ctx, x: LegProgress, cl: ClassifiedLeg, *, last: bool) -> AlertEvent:
    """7.9 `delayed`: silent, loud when the last leg of the outbound journey arrives after
    `latest_acceptable_arrival` (compared as instants, 7.13). A broken change is its own loud
    event. Departure and arrival delays are each stepped by `_stated`, so only a 3-min move of
    either is news, and a fall back to on time becomes its recovery message (7.5)."""
    dep, arr_late = cl.dep_delay_min, cl.arr_delay_min
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop), None)
    arr = None if stop is None or stop.arrival is None else stop.arrival.value
    planned = None if stop is None or stop.arrival is None else stop.arrival.planned
    limit = ctx.profile.alerts.latest_acceptable_arrival
    past = (
        last
        and ctx.direction is Direction.OUTBOUND
        and limit is not None
        and arr is not None
        and _utc(arr) > _utc(_limit_at(limit, planned or arr))
    )
    if dep is not None and arr_late is not None and dep != arr_late:
        on = "on time" if arr_late <= ON_TIME_MAX_MIN else f"{arr_late} min late"
        text = f"your {_name(x)} train leaves {dep} min late and arrives {on}"
    else:
        text = (
            f"your {_name(x)} train is delayed {arr_late if arr_late is not None else dep or 0} min"
        )
    if arr is not None:
        text += f", arriving {_hhmm(arr)}"
    sev = Severity.LOUD if past else Severity.SILENT

    def n(v: int | None) -> str:
        return "?" if v is None else str(v)

    facts = f"{_name(x)}|d~{n(dep)}|a~{n(arr_late)}"
    return _event(ctx, EventKind.DELAYED, sev, f"{text}.", x.train, facts)


def _platform(ctx: Ctx, x: LegProgress, cl: ClassifiedLeg) -> AlertEvent | None:
    """7.9 `platform_change` for a leg not yet left: loud within 15 min of its expected
    departure, else silent (escalated once it enters the zone, rule 4)."""
    b = cl.leg.board
    if not b.actual_track or not b.planned_track:
        return None
    soon = _departs(x, cl) - _utc(ctx.now) <= _PLATFORM_LOUD
    sev = Severity.LOUD if soon else Severity.SILENT
    if b.actual_track == b.planned_track:  # `_stated` keeps it only after an announced change
        text = f"your {_name(x)} train leaves from platform {b.planned_track} again, as planned."
        facts = f"track {b.actual_track}|planned"
    else:
        text = f"your {_name(x)} train now leaves from platform {b.actual_track} (was {b.planned_track})."
        facts = f"track {b.actual_track}"
    return _event(ctx, EventKind.PLATFORM_CHANGE, sev, text, x.train, facts)


def _follow(
    s: OccurrenceState,
    ctx: Ctx,
    pool: _Pool,
    asked: tuple[Query, ...],
    *,
    relock: bool = True,
) -> Transition:
    """The locked-journey checks (7.3, 7.4). A warning never depends on permission to move a
    train: a break at a train the user may be on, or anywhere in a retained remainder
    (`relock` False, rule 7), is reported with the next good connection and moves nothing;
    only observed progress (endpoints, onward search time) is kept."""
    lock = s.lock
    assert lock is not None
    p, d = ctx.profile, ctx.direction
    dest, now = p.destination(d), ctx.now
    legs = list(lock.legs)
    k = _first_open(lock)
    seen = [_observe(pool, x) for x in legs]
    obs = [o[0] for o in seen]
    # a served-endpoint recovery is not the planned pattern, so it is not a route change
    plain = all(x.alight_stop == x.intended_alight for x in legs[k:])
    expected = tuple(x.alight_stop for x in legs[k:-1])
    rc = route_changed([x.train for x in legs[k:]], pool, expected) if plain else None
    events: list[AlertEvent] = []

    def act(t: Transition, ridden: bool = False) -> Transition | None:
        """The relock `t` when allowed; else None, keeping its warnings, and every later leg
        is still checked and reported but never relocked on top of the kept break (7.4)."""
        nonlocal relock
        if relock and not ridden:
            # a platform of a train relocked away is no longer the user's platform
            kept = {y.train for y in t.state.lock.legs} if t.state.lock else set()
            mine = [e for e in events if e.kind is not EventKind.PLATFORM_CHANGE or e.train in kept]
            return Transition(t.state, (*mine, *t.events))
        relock = False
        events.extend(t.events)
        return None

    def over(j: int) -> Connection | None:
        """The actual route of the boarded kept leg legs[j - 1], if NS reroutes it."""
        boarded = j > 0 and _may_be_on(legs[j - 1], now)
        return rc if rc is not None and boarded and rc.legs[0].train == legs[j - 1].train else None

    def since(j: int) -> datetime | None:
        return _since(
            _late_onward(legs[j - 1], legs[j]), _late_onward(lock.legs[j - 1], lock.legs[j])
        )

    # from the incoming leg of the next transfer: a done leg stays watched, so its endpoint and
    # its change into the next leg are still checked (rule 6, lifecycle 26)
    for i in range(max(k - 1, 0), len(legs)):
        x, cl = legs[i], obs[i]
        if not _observed(x, now):
            continue
        nxt = legs[i + 1] if i + 1 < len(legs) else None
        ncl = obs[i + 1] if nxt is not None else None
        # at risk warns before a change; once the onward train left, only broken is news
        ahead = nxt is not None and nxt.state not in _DEPARTED
        # 6.1, 7.3: an uncertain leg makes its change at risk, never fine, whatever progress says
        unsure = Observation.UNCERTAIN in (seen[i][1], seen[i + 1][1]) if nxt else False
        if nxt is not None and nxt.state is not LegState.DONE and unsure:
            events.append(_at_risk(ctx, x, nxt, "a train is not reliably identified"))
        if cl is None:  # missing or uncertain: reported, never relocked on
            continue
        if cl.kind is LegKind.CANCELLED and x.state is not LegState.DONE:
            text = f"your {_name(x)} train is cancelled"
            kept = obs[i - 1] if i else None
            cause = (EventKind.CANCELLED, Severity.LOUD, text, x.train)
            t = _relock(
                s,
                ctx,
                pool,
                asked,
                legs,
                i,
                x.board_station,
                kept,
                cause,
                x,
                None,
                since(i) if i else None,
            )
            if (r := act(t, ridden=x.state is LegState.RIDING)) is not None:
                return r
            continue
        usable = cl.kind in _RUNS or cl.kind is LegKind.CUT_SHORT
        if usable and x.state in _MAY_HAVE_LEFT and (pc := _platform(ctx, x, cl)):
            events.append(pc)
        cut = cl.kind is LegKind.CUT_SHORT or x.alight_stop != x.intended_alight
        before = x
        if cut:
            last = cl.last_served
            end = (
                last.code
                if last is not None
                else x.intended_alight
                if cl.kind in _RUNS
                else x.served_endpoint
            )
            # frozen once a recovery leg is boarded, by its departure evidence (7.4)
            frozen = x.alight_frozen or (nxt is not None and nxt.state in _DEPARTED)
            x = _move_stop(x, end, x.alight_stop if frozen else end)
            x = legs[i] = replace(x, alight_frozen=frozen)
        onward = _onward(p, x, cl, ncl)
        if onward is not None:
            x = legs[i] = replace(x, onward_from=onward)
        if cut and not x.alight_frozen:
            end = x.served_endpoint
            cause = (
                (
                    EventKind.CUT_SHORT,
                    Severity.LOUD,
                    f"your {_name(x)} train runs only to {end}, change there",
                    x.train,
                )
                if cl.kind is LegKind.CUT_SHORT
                else (
                    EventKind.RESTORED,
                    Severity.SILENT,
                    f"back to normal: your {_name(x)} train reaches {end} again",
                    x.train,
                )
            )
            if nxt is not None and end == dest:  # extended to the destination again
                new = _journey(lock, legs[: i + 1], (), LockSource.RESTORE)
                kind, sev, text, train = cause
                note = _event(ctx, kind, sev, f"{text}.", train)
                if (r := act(Transition(_write(s, new), (note,)))) is not None:
                    return r
                continue
            if (nxt is None and end != dest) or (nxt is not None and nxt.board_station != end):
                original = (
                    nxt if nxt is not None and nxt.board_station == x.intended_alight else None
                )
                asked_from = before.onward_from if before.alight_stop == end else None
                t = _relock(
                    s,
                    ctx,
                    pool,
                    asked,
                    legs,
                    i + 1,
                    end,
                    cl,
                    cause,
                    original,
                    over(i + 1),
                    _since(x.onward_from, asked_from),
                )
                if (r := act(t)) is not None:
                    return r
                continue
        # every running leg: `_stated` keeps only news, including the fall back to on time
        late = cl.kind in _RUNS and x.state is not LegState.DONE
        if nxt is None or ncl is None or ncl.kind is LegKind.CANCELLED:
            if late and ncl is None:  # a cancelled next leg is the news, not this delay
                events.append(_delayed(ctx, x, cl, last=nxt is None))
            continue  # a cancelled next leg is handled as a cancellation on its own turn
        tc = check_transfer(cl, ncl, x.alight_stop, p.transfer_min)
        if tc.state is TransferState.BROKEN:
            text = f"your change at {x.alight_stop} is broken: {tc.reason}"
            cause = (EventKind.TRANSFER_BROKEN, Severity.LOUD, text, nxt.train)
            t = _relock(
                s,
                ctx,
                pool,
                asked,
                legs,
                i + 1,
                x.alight_stop,
                cl,
                cause,
                None,
                over(i + 1),
                since(i + 1),
            )
            if (r := act(t, ridden=nxt.state in _DEPARTED)) is not None:
                return r
            continue
        if late:  # after the break check: a broken change is its own loud event
            events.append(_delayed(ctx, x, cl, last=False))
        if tc.state is TransferState.AT_RISK and ahead:
            events.append(_at_risk(ctx, x, nxt, tc.reason))
    if rc is not None and obs[k] is not None:
        x = legs[k]
        via = ", ".join(rc.transfer_stations) or "a direct train"
        text = f"NS now routes your {_name(x)} train via {via}"
        cause = (EventKind.ROUTE_CHANGED, Severity.LOUD, text, x.train)
        kept = obs[k - 1] if k else None
        if not _may_be_on(x, now):
            # 6.3: follow the new route only within the cap and if good, else relock
            checkable = not k or kept is not None
            if checkable and is_good(rc, pool, **_good_kw(s, ctx, x.board_station, kept, k)).ok:
                new = _journey(lock, legs[:k], rc.legs, LockSource.RELOCK)
                note = _event(
                    ctx, EventKind.ROUTE_CHANGED, Severity.LOUD, f"{text}, following it.", x.train
                )
                r = act(Transition(_write(s, new), (note,)))
            else:
                r = act(_relock(s, ctx, pool, asked, legs, k, x.board_station, kept, cause))
        else:
            # a boarded train is never dropped: relock what follows from where NS changes
            end = rc.legs[0].alight.code
            legs[k] = _move_stop(x, end, end)
            r = act(_relock(s, ctx, pool, asked, legs, k + 1, end, obs[k], cause, None, rc))
        if r is not None:
            return r
    return Transition(_progress(s, legs), tuple(events))


def _departs(x: LegProgress, cl: ClassifiedLeg) -> datetime:
    """Expected departure, by the observed time when there is one."""
    d = cl.leg.board.departure
    t = d.value if d is not None and d.value is not None else x.train.planned_departure
    return _utc(t)


def _advance(ctx: Ctx, pool: _Pool, j: Journey) -> tuple[Journey, list[int]]:
    """Leg progress (7.7): boarding within the buffer of the expected departure, riding once it
    has departed. A train cancelled at its boarding stop is never boarded. A train not
    identified in this poll gets no progress: RIDING is departure evidence (it freezes an
    alighting stop, 7.4), while `_may_be_on` already retains it by its planned time. Returns
    the journey and the indexes of the legs that departed now."""
    now = _utc(ctx.now)
    buffer = timedelta(minutes=ctx.profile.journey(ctx.direction).buffer_min)
    legs, left = list(j.legs), list[int]()
    for i, x in enumerate(legs):
        if x.state not in _MAY_HAVE_LEFT:
            continue
        cl = _observe(pool, x)[0]
        if cl is None or cl.kind is LegKind.CANCELLED:
            continue
        dep = _departs(x, cl)
        if now >= dep:
            legs[i], left = replace(x, state=LegState.RIDING), [*left, i]
        elif now >= dep - buffer and x.state is LegState.PENDING:
            legs[i] = replace(x, state=LegState.BOARDING)
    return replace(j, legs=tuple(legs)), left


def _left_note(s: OccurrenceState, ctx: Ctx, pool: _Pool, j: Journey, i: int) -> AlertEvent:
    """A locked train departed with no missed-leg report: keep following it and show the next
    good connection in case the user is not on it (7.7, rule 7)."""
    x = j.legs[i]
    kept = _observe(pool, j.legs[i - 1])[0] if i else None
    text = f"your {_name(x)} train has left. If you are not on it, "
    if i and kept is None:
        text += "check the NS app: the change from your current train is not in NS data."
    else:
        c = _next_good(s, ctx, pool, x.train, x.board_station, kept, i)
        text += (
            f"next good connection: {_hhmm(_departure(c))}."
            if c is not None
            else "no good connection found, check the NS app."
        )
    return _event(ctx, EventKind.NEXT_CONNECTION, Severity.SILENT, text, x.train)


def _advance_all(
    s: OccurrenceState, ctx: Ctx, pool: _Pool, live: bool
) -> tuple[OccurrenceState, list[AlertEvent]]:
    events: list[AlertEvent] = []
    lock = s.lock
    if live and lock is not None:
        lock, left = _advance(ctx, pool, lock)
        events = [_left_note(s, ctx, pool, lock, i) for i in left]
    retained = tuple(_advance(ctx, pool, r)[0] for r in s.retained)
    # riding once leg 1 departs (7.7); a retained remainder exists only for a departed train
    riding = bool(retained) or (lock is not None and any(x.state in _DEPARTED for x in lock.legs))
    state = s.direction_state
    if riding and state in (DirectionState.IDLE, DirectionState.LOCKED):
        state = DirectionState.RIDING
    return replace(s, lock=lock, retained=retained, direction_state=state), events


def on_poll(s: OccurrenceState, ctx: Ctx, board: Board) -> Transition:
    """Apply one poll (7.4, rule 2). A result started under another generation is discarded
    (rule 21). One lock change per poll; the next poll, at the new generation, sees the rest.
    Ongoing facts (`missing`, `transfer_at_risk`, retained ones keyed `...:retained:<n>`) repeat
    every poll with a stable key: the news ledger (7.5) must drop repeats by key. Arrival
    acceptance (rule 6) and leg progress (7.7) run first; a lock whose journey ended (`DONE`)
    is no longer followed, while its retained remainders still are (rule 7). Any plan change
    raises the generation once (rule 21)."""
    if board.started_generation != s.generation:
        return Transition(s, ())
    start = s
    s = replace(s, poll_records=(*s.poll_records, *board.polls)[-_KEEP_POLLS:])
    if (s.lock is None and not s.retained) or s.not_travelling or not board.answers:
        return Transition(s, ())  # before a lock, auto_lock judges the usual
    pool = _pool(board.answers)
    live = s.lock is not None and s.direction_state is not DirectionState.DONE
    s, events = _accept_all(s, ctx, pool, live)
    s, more = _advance_all(s, ctx, pool, live)
    events += more
    for j in ((s.lock,) if live and s.lock else ()) + s.retained:
        on = (
            "" if j is s.lock else f"if you are on the {_hhmm(j.legs[0].train.planned_departure)}: "
        )
        for x in j.legs:
            if _observed(x, ctx.now) and _observe(pool, x)[1] is Observation.MISSING:
                text = f"{on}your {_name(x)} train is not in NS data."
                events.append(_event(ctx, EventKind.MISSING, Severity.LOUD, text, x.train))
    asked = tuple(a.query for a in board.answers)
    retained = []
    for r in s.retained:
        # rule 7: a retained remainder gets the locked-journey checks but is never relocked, so
        # it keeps its observed progress and what the checks say, with the next good connection
        first = r.legs[0].train
        on = f"if you are on the {_hhmm(first.planned_departure)}: "
        alone = replace(s, lock=r, retained=(), cancellation_history=())
        t = _follow(alone, ctx, pool, asked, relock=False)
        assert t.state.lock is not None
        retained.append(t.state.lock)
        for e in t.events:
            head, _, body = e.text.partition(": ")
            key = f"{e.key}:retained:{first.train_number}@{first.planned_departure.isoformat()}"
            events.append(replace(e, text=f"{head}: {on}{body}", key=key))
    s = replace(s, retained=tuple(retained))
    t = (_restore(s, ctx, pool) or _follow(s, ctx, pool, asked)) if live else Transition(s, ())
    return _stated(_bumped(start, t.state), (*events, *t.events))


# --- arrival acceptance, location, missed legs, termination (7.2, 7.7, 7.12 rules 6, 7) ---

STALE_LOCATION = timedelta(minutes=10)
ERRAND_TIMEOUT = timedelta(minutes=45)
ACCEPT_AFTER = timedelta(minutes=5)
ACCEPT_APART = timedelta(minutes=4)
LAST_WATCH = timedelta(minutes=30)
FOLLOW_LIMIT = timedelta(minutes=150)
_NOT_ACCEPTABLE = (LegState.CANCELLED, LegState.RELOCKED)
_MISSABLE = (LegState.PENDING, LegState.BOARDING, LegState.RIDING)


def _accept(
    ctx: Ctx, x: LegProgress, pool: _Pool, on: str
) -> tuple[LegProgress, AlertEvent | None]:
    """Rule 6: accepted after two healthy answers at least 4 min apart, both at least 5 min after
    the reported arrival, show the same arrival; an accepted leg reopens when the arrival moves
    later or the train no longer reaches its alighting stop."""
    cl, _, seen = _observe(pool, x)
    if x.state in _NOT_ACCEPTABLE or cl is None or seen is None:  # missing or uncertain: nothing
        return x, None
    stop = next((st for st in cl.leg.stops[1:] if st.code == x.alight_stop), None)
    # passing, or cancelled without a reported time, is not served (classify.served)
    bad = cl.kind is LegKind.CANCELLED or stop is None or not served(stop)
    arr = None if stop is None else stop.arrival
    v = arr.value if arr is not None and arr.provenance is Provenance.REPORTED else None
    reset = replace(x, accept_value=None, accept_first_seen=None)
    if x.state is LegState.DONE:
        old = x.accept_value
        later = v is not None and old is not None and _utc(v) > _utc(old)
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
            return reopened, _event(ctx, kind, Severity.LOUD, text, x.train)
        assert v is not None
        text = f"{on}NS now shows your {_name(x)} train arriving at {_hhmm(v)}; following it again."
        return reopened, _event(ctx, EventKind.DELAYED, Severity.SILENT, text, x.train)
    if bad or v is None:
        return reset, None
    # the time of the answer that showed this arrival, never another query's (rule 6)
    if _utc(seen) < _utc(v) + ACCEPT_AFTER:
        return reset, None  # an estimate before its time: never acceptance
    first = x.accept_first_seen
    if x.accept_value is None or _utc(x.accept_value) != _utc(v) or first is None:
        return replace(x, accept_value=v, accept_first_seen=seen), None
    if _utc(seen) - _utc(first) >= ACCEPT_APART:
        return replace(x, state=LegState.DONE, accepted_at=ctx.now), None
    return x, None


def _accept_journey(ctx: Ctx, j: Journey, pool: _Pool, on: str) -> tuple[Journey, list[AlertEvent]]:
    pairs = [_accept(ctx, x, pool, on) for x in j.legs]
    legs = [x for x, _ in pairs]
    last = legs[-1]
    # every earlier watch ends with the last one (rule 6); a reopened last leg reopens them all
    w = None
    if last.state is LegState.DONE and last.accepted_at is not None:
        w = last.watch_until or (_utc(last.accepted_at) + LAST_WATCH).astimezone(TZ)
    legs = [replace(x, watch_until=w if x.state is LegState.DONE else None) for x in legs]
    return replace(j, legs=tuple(legs)), [e for _, e in pairs if e is not None]


def _accept_all(
    s: OccurrenceState, ctx: Ctx, pool: _Pool, live: bool
) -> tuple[OccurrenceState, list[AlertEvent]]:
    events: list[AlertEvent] = []
    lock = s.lock
    if live and lock is not None:
        lock, es = _accept_journey(ctx, lock, pool, "")
        events += es
    retained = []
    for r in s.retained:
        on = f"if you are on the {_hhmm(r.legs[0].train.planned_departure)}: "
        r, es = _accept_journey(ctx, r, pool, on)
        retained.append(r)
        events += es
    return replace(s, lock=lock, retained=tuple(retained)), events


def _place_zone(p: Profile, station: str) -> str:
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
    first station zone a definitive one, a station report after a leg's expected departure is a
    missed leg (rule 7). `answer`: a fresh healthy answer from the origin; without it only the
    travelling and station facts are kept, so the runtime passes the origin answer with every
    event. `watched`: the answers of `location_queries`, read only for a missed leg. A stale
    event counts as unknown. Only an `enter` or `report` is kept in `zone_reports` (presence).
    Nothing here ends or silences anything."""
    unchanged = Transition(s, ())
    if _resolved(s) or event.zone is None:
        return unchanged
    if _utc(ctx.now) - _utc(event.at) > STALE_LOCATION:
        return unchanged
    start = s
    prev = s.zone_reports.get(event.zone)
    # a leave places the user outside the zone: it is never presence (rule 6, 8.4)
    if event.kind != "leave" and (prev is None or _utc(event.at) > _utc(prev)):
        s = replace(s, zone_reports={**s.zone_reports, event.zone: event.at})
    t = _locate(s, ctx, event, answer, watched)
    # rule 21: a lock, catchability or missed-leg change raises the generation; a repeat
    # report that changes none of them keeps polls in flight
    return _stated(_bumped(start, t.state), t.events)


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
        if event.zone == _place_zone(p, p.origin(d)):
            return _left_origin(s, ctx, event, answer)
        return Transition(s, ())
    pool = None if answer is None else _pool((answer,))
    seen = (*((answer,) if answer is not None else ()), *watched)  # `location_queries`
    missed = _missed(s, ctx, event, _pool(seen)) if seen else None
    if missed is not None:
        return missed
    if event.zone == p.origin(d):
        return _at_first_station(s, ctx, pool)
    return Transition(s, ())


def _left_origin(
    s: OccurrenceState, ctx: Ctx, event: LocationEvent, answer: ProviderAnswer | None
) -> Transition:
    s = replace(s, travelling=True, tap_at=event.at)  # catchability: event time + buffer
    lock = s.lock
    if answer is None or (lock is not None and lock.source is not LockSource.AUTO):
        return Transition(s, ())  # a chosen, station or later lock is kept
    c = _next_good(s, ctx, answer)
    if c is None:
        return Transition(s, ())
    deadline = (_utc(event.at) + ERRAND_TIMEOUT).astimezone(TZ)
    new = journey_of(c, LockSource.LOCATION, Commitment.PROVISIONAL)
    written = write_lock(replace(s, errand_deadline=deadline), new, expect=lock, now=ctx.now)
    assert written is not None  # only an auto lock or none is replaced
    return Transition(written, ())


def _at_first_station(s: OccurrenceState, ctx: Ctx, pool: _Pool | None) -> Transition:
    """Commit the lock; an auto or location lock is replaced by a station lock (7.2)."""
    lock = s.lock
    s = replace(s, at_station=True, travelling=True, errand_deadline=None)
    new = None if lock is None else replace(lock, commitment=Commitment.COMMITTED)
    if pool is not None and (lock is None or lock.source in _STATION_REPLACES):
        c = _next_good(s, ctx, pool)
        if c is not None and (
            lock is None or [x.train for x in c.legs] != [x.train for x in lock.legs]
        ):
            written = write_lock(
                s, journey_of(c, LockSource.STATION, Commitment.COMMITTED), expect=lock, now=ctx.now
            )
            assert written is not None
            return Transition(written, ())
        if c is not None and new is not None:  # the same train: only the source changes
            new = replace(new, source=LockSource.STATION)
    return Transition(s if new is None else replace(s, lock=new), ())


def _missed(s: OccurrenceState, ctx: Ctx, event: LocationEvent, pool: _Pool) -> Transition | None:
    """Rule 7: a report at a leg's boarding station time-stamped after its expected departure.
    The leg is marked missed, so the lock writer keeps it a retained remainder."""
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
    cl = _observe(pool, x)[0]
    if cl is None or cl.kind is LegKind.CANCELLED:
        return None  # expected departure unknown, or a cancellation the follow step relocks
    dep = cl.leg.board.departure
    expected = dep.value if dep is not None and dep.value is not None else x.train.planned_departure
    if _utc(event.at) <= _utc(expected):
        return None
    legs[i] = replace(x, state=LegState.MISSED)
    first = i == 0
    s = replace(
        s,
        lock=replace(lock, legs=tuple(legs), commitment=Commitment.COMMITTED),
        at_station=s.at_station or first,
        travelling=True,
        errand_deadline=None,
    )
    if first:
        cause = (
            EventKind.MISSED_FIRST_TRAIN,
            Severity.LOUD,
            f"you missed your {_name(x)} train",
            x.train,
        )
        kept = None
    else:
        text = f"you missed your change at {x.board_station} to the {_name(x)} train"
        cause = (EventKind.MISSED_TRANSFER, Severity.LOUD, text, x.train)
        kept = _observe(pool, legs[i - 1])[0]
    asked = tuple(a.query for a in pool.answers)
    return _relock(
        s, ctx, pool, asked, legs, i, x.board_station, kept, cause, source=LockSource.MISSED
    )


def on_tick(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer | None = None) -> Transition:
    """Time-driven steps: the errand timeout (45 min) clears a provisional lock, keeping a
    departed train retained; the auto lock when due (`answer` is the usual's query answer); a
    window closed with no lock; each journey's end by its last watch or its 150-min limit
    (rules 6, 7). Arrival acceptance itself runs on each poll. The runtime passes the usual's
    answer on every tick from `auto_lock_at` on; without it no auto lock is written."""
    if s.not_travelling:
        return Transition(s, ())
    t = _tick(s, ctx, answer)
    return Transition(_bumped(s, t.state), t.events)  # termination and removal: rule 21


def _tick(s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer | None) -> Transition:
    now = _utc(ctx.now)
    events: list[AlertEvent] = []
    lock = s.lock
    if (
        lock is not None
        and lock.commitment is Commitment.PROVISIONAL
        and s.errand_deadline is not None
        and now >= _utc(s.errand_deadline)
    ):
        f = _first_open(lock)
        retained = s.retained
        if f < len(lock.legs) and _may_be_on(lock.legs[f], now):
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
        and now >= _utc(ctx.schedule.window_end)
    ):
        text = "I stopped following your journey, check the NS app."
        events.append(_event(ctx, EventKind.STOPPED_FOLLOWING, Severity.LOUD, text))
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
    assert s.lock is not None
    legs = list(s.lock.legs)
    trains = [x.train for x in legs]
    kept = []
    for r in s.retained:
        rest = r.legs[_first_open(r) :]
        i = trains.index(rest[0].train) if rest and rest[0].train in trains else None
        if i is None or trains[i:] != [x.train for x in rest]:
            kept.append(r)
            continue
        legs[i:] = rest
    return replace(s, lock=replace(s.lock, legs=tuple(legs)), retained=tuple(kept))


def _arrived(s: OccurrenceState, ctx: Ctx, last: LegProgress) -> bool:
    """A location report time-stamped after the accepted arrival, at the alighting station or
    the destination zone (rule 6)."""
    when = last.accept_value or last.accepted_at
    if when is None:
        return False
    p, d = ctx.profile, ctx.direction
    zones = {last.alight_stop, p.destination(d), _place_zone(p, p.destination(d))}
    return any((t := s.zone_reports.get(z)) is not None and _utc(t) > _utc(when) for z in zones)


def _journey_end(
    s: OccurrenceState, ctx: Ctx, j: Journey, *, retained: bool
) -> tuple[AlertEvent, ...] | None:
    """None while `j` is followed; else the events of its end: silent with arrival evidence,
    else one loud `stopped_following` naming it (OQ4 = A)."""
    now = _utc(ctx.now)
    last = j.legs[-1]
    first = j.legs[0].train
    on = f"if you are on the {_hhmm(first.planned_departure)}: " if retained else ""
    train = first if retained else None
    limit = _utc(j.planned_final_arrival) + FOLLOW_LIMIT
    if last.state is LegState.DONE:
        # the earlier of the last watch and the journey's 150-min limit (rules 6, 7)
        w = last.watch_until
        if now < limit and (w is None or now < _utc(w)):
            return None
        if _arrived(s, ctx, last):
            return ()
        arrival = _hhmm(last.accept_value or last.accepted_at or w or limit)
        text = (
            f"{on}NS showed it arriving at {arrival}; I stopped following it, check the NS app."
            if retained
            else f"NS showed your train arriving at {arrival}; I stopped following, check the NS "
            "app if you are not there."
        )
        return (_event(ctx, EventKind.STOPPED_FOLLOWING, Severity.LOUD, text, train),)
    if now < limit:
        return None
    text = f"{on}I stopped following {'it' if retained else 'your journey'}, check the NS app."
    return (_event(ctx, EventKind.STOPPED_FOLLOWING, Severity.LOUD, text, train),)


def ends(s: OccurrenceState, now: datetime, pending_journey_warnings: int) -> bool:
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
    limit = _utc(s.lock.planned_final_arrival) + FOLLOW_LIMIT
    return w is None or _utc(now) >= min(_utc(w), limit)

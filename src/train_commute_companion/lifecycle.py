"""Occurrence state, the lock writer, user actions and the follow step (7.2, 7.4, 7.8, 7.12 rules
1, 2, 3, 8, 10, 11, 21, 7.13). Pure: `now` always comes from the caller."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
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
from train_commute_companion.calendar import TZ
from train_commute_companion.classify import classify_leg, find_leg
from train_commute_companion.health import PollRecord
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
    Severity,
    Signature,
    TrainId,
    TransferState,
)
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.transfer import check_transfer


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
_NEEDS_EXPECT_OVER_CHOICE = (LockSource.RELOCK, LockSource.RESTORE)
_UNRESOLVED_END = (DirectionState.DONE, DirectionState.EXPIRED)


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
    done = () if old is None else tuple(x for x in old.legs if x.state is LegState.DONE)
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
        if replaced and _may_be_on(old.legs[replaced[0]], now):
            retained += (replace(old, legs=old.legs[replaced[0] :]),)
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


def _event(
    ctx: Ctx, kind: EventKind, severity: Severity, text: str, train: TrainId | None = None
) -> AlertEvent:
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
        f"{kind.value}:{key.service_date.isoformat()}{ident}",
        ctx.now,
    )


def _next_good(
    s: OccurrenceState, ctx: Ctx, answer: ProviderAnswer, after: TrainId | None = None
) -> Connection | None:
    j = ctx.profile.journey(ctx.direction)
    others = ProviderAnswer(
        answer.query,
        tuple(c for c in answer.connections if after is None or c.legs[0].train != after),
        answer.fetched_at,
    )
    c, _ = choose_replacement(
        others,
        usual_remaining=j.transfers,
        board_station=ctx.profile.origin(ctx.direction),
        destination=ctx.profile.destination(ctx.direction),
        catch=catch_from_state(s, j.buffer_min, ctx.now),
        kept_incoming=None,
        done_transfers=0,
        max_transfers=max_transfers(j.transfers),
        profile_min=ctx.profile.transfer_min,
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
        if _departure(usual) < now:
            # the user may be on it; show where to go if not (7.12 rule 7)
            nxt = _next_good(s, ctx, answer, after=usual.legs[0].train)
            text = f"your usual {_hhmm(ctx.schedule.first_departure)} has left. " + (
                f"If you are not on it, next good connection: {_hhmm(_departure(nxt))}."
                if nxt is not None
                else "If you are not on it, no good connection found, check the NS app."
            )
            events.append(_event(ctx, EventKind.NEXT_CONNECTION, Severity.SILENT, text))
        return Transition(locked, tuple(events))
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
        return next((x for x in states if x.key.service_date == action.service_date), None)
    open_ = sorted(
        (x for x in states if x.direction_state not in _UNRESOLVED_END),
        key=lambda x: x.key.service_date,
    )
    if isinstance(action, NotTravelling):
        return next(iter(open_), None)
    if isinstance(action, Choose):
        first = action.connection.legs[0].train
        # no clock here: a lock leg counts by its state (the user "rides" it, 7.13)
        riding = next((x for x in open_ if _ridden_source(x, first, None) is not None), None)
        if riding is not None:
            return riding
    return next((x for x in open_ if x.direction_state is not DirectionState.RIDING), None)


def _refuse(s: OccurrenceState, ctx: Ctx, reason: str) -> Transition:
    text = f"choice refused: {reason}."
    return Transition(s, (_event(ctx, EventKind.CHOOSE_REFUSED, Severity.LOUD, text),))


def _choose(s: OccurrenceState, ctx: Ctx, c: Connection) -> Transition:
    p, d = ctx.profile, ctx.direction
    j = p.journey(d)
    done = () if s.lock is None else tuple(x for x in s.lock.legs if x.state is LegState.DONE)
    source = _ridden_source(s, c.legs[0].train, ctx.now)
    if source is not None and len(c.legs) == 1:
        check = GoodCheck(c.destination == p.destination(d), "does not arrive at the destination")
        if check.ok and len(done) > max_transfers(j.transfers):
            check = GoodCheck(False, "over the transfer cap")
    else:
        # the shell's copy of a fresh answer, checked on its own
        answer = ProviderAnswer(Query(c.origin, c.destination, ctx.now), (c,), ctx.now)
        # a train the user may be on is kept: catchability applies only to the legs after it
        rest = c if source is None else Connection(c.legs[1:], c.status, c.ctx_recon)
        check = is_good(
            rest,
            answer,
            board_station=rest.origin if source else done[-1].alight_stop if done else p.origin(d),
            destination=p.destination(d),
            catch=catch_from_state(s, j.buffer_min, ctx.now)
            if source is None
            else Catchability(ctx.now.astimezone(UTC)),
            kept_incoming=None if source is None else classify_leg(c.legs[0], rest.origin),
            done_transfers=len(done),
            max_transfers=max_transfers(j.transfers),
            profile_min=p.transfer_min,
        )
    if not check.ok:
        text = f"choice refused: {check.reason}."
        return Transition(s, (_event(ctx, EventKind.CHOOSE_REFUSED, Severity.LOUD, text),))
    riding = source is not None or s.travelling
    new = journey_of(c, LockSource.CHOOSE, Commitment.COMMITTED if riding else Commitment.INTENDED)
    if source is not None:  # the ridden prefix keeps its progress
        have = {x.train: x for x in source.legs}
        new = replace(new, legs=tuple(have.get(x.train) or progress_of(x) for x in c.legs))
    # a choice ends every retained remainder; the writer retains the old lock if ridden
    written = write_lock(replace(s, retained=()), new, expect=s.lock, now=ctx.now)
    assert written is not None  # a choice always writes over the lock it read
    return Transition(written, ())


def apply_action(s: OccurrenceState, ctx: Ctx, action: UserAction) -> Transition:
    if isinstance(action, Choose):
        return _choose(s, ctx, action.connection)
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
_KEEP_POLLS = 20  # shortcut: health reads only the latest records; keep more if it ever needs a day


@dataclass(frozen=True, slots=True)
class Board:
    answers: tuple[ProviderAnswer, ...]  # healthy answers only: a failed request raised
    polls: tuple[PollRecord, ...]
    started_generation: int


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


def queries_needed(s: OccurrenceState, ctx: Ctx) -> list[Query]:
    """Rule 2. Each query carries a full date-time, so a leg after midnight is asked on its own
    date (7.13). The first query is from the first leg not done."""
    p, d = ctx.profile, ctx.direction
    dest = p.destination(d)
    out: list[Query] = []

    def add(station: str, at: datetime) -> None:
        q = Query(station, dest, at)
        if q not in out:
            out.append(q)

    if s.lock is None:  # the usual at its effective departure, also when past (6.3)
        add(p.origin(d), ctx.schedule.first_departure)
        return out
    k = _first_open(s.lock)
    if k < len(s.lock.legs):
        add(s.lock.legs[k].board_station, s.lock.legs[k].train.planned_departure)
    for j in (s.lock, *s.retained):
        k = _first_open(j)
        for i, x in enumerate(j.legs):
            if _observed(x, ctx.now) or i == k - 1:  # k - 1: the next transfer's incoming leg
                add(x.board_station, x.train.planned_departure)
                if x.alight_stop != x.intended_alight:  # recovery search from a served endpoint
                    add(x.alight_stop, x.train.planned_departure)
            nxt = j.legs[i + 1] if i + 1 < len(j.legs) else None
            late = _late_onward(x, nxt)
            if late is not None:  # a delay broke the change: search after the expected arrival
                add(x.alight_stop, late)
    for h in s.cancellation_history:
        if _restorable(s, ctx, h):
            add(h.board_station, h.train.planned_departure)
    return out


def _pool(answers: tuple[ProviderAnswer, ...]) -> ProviderAnswer:
    """All answers plus every connection's suffixes, so a search from a transfer station can use
    an answer asked from earlier on the route (rule 3)."""
    conns = [c for a in answers for c in a.connections]
    frags = [Connection(c.legs[i:], c.status, None) for c in conns for i in range(1, len(c.legs))]
    fetched = max(a.fetched_at for a in answers)
    return ProviderAnswer(answers[0].query, (*conns, *frags), fetched)


def _observe(pool: ProviderAnswer, x: LegProgress) -> tuple[ClassifiedLeg | None, Observation]:
    leg, ob = find_leg(pool, x.train, x.board_station)
    if leg is None:
        return None, ob
    # one train shows in several connections, each cut where it changes: take the farthest run
    runs = [
        classify_leg(g, x.intended_alight)
        for c in pool.connections
        for g in c.legs
        if g.train == leg.train and g.board.code == x.board_station
    ]
    best = max(runs, key=lambda c: (c.kind not in _BAD, len(c.leg.stops)))
    return replace(best, observation=ob), ob


def _name(x: LegProgress) -> str:
    return f"{_hhmm(x.train.planned_departure)} {x.board_station} to {x.intended_alight}"


def _good_kw(
    s: OccurrenceState, ctx: Ctx, station: str, kept: ClassifiedLeg | None, n_before: int
) -> GoodKwargs:
    """`n_before` legs stay before the new suffix; the last of them is `kept`, if any."""
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
    assert s.lock is not None
    if tuple(legs) == s.lock.legs:
        return s

    def plan(xs: Sequence[LegProgress]) -> list[LegProgress]:
        return [replace(x, onward_from=None) for x in xs]

    # a new observed time alone is not a lock change, so polls in flight stay valid (rule 21)
    moved = plan(legs) != plan(s.lock.legs)
    return replace(s, lock=replace(s.lock, legs=tuple(legs)), generation=s.generation + moved)


def _write(s: OccurrenceState, new: Journey) -> OccurrenceState:
    # None: every caller has checked that the replaced train is not ridden
    written = write_lock(s, new, expect=s.lock, now=None)
    assert written is not None  # relock and restore pass the lock just read as `expect`
    return written


def _restore(s: OccurrenceState, ctx: Ctx, pool: ProviderAnswer) -> Transition | None:
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
                prefix[-1] = replace(prefix[-1], served_endpoint=station, alight_stop=station)
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
    pool: ProviderAnswer,
    asked: tuple[Query, ...],
    legs: Sequence[LegProgress],
    j: int,
    station: str,
    kept: ClassifiedLeg | None,
    cause: Cause,
    history: LegProgress | None = None,
    over_cap: Connection | None = None,
    since: datetime | None = None,
) -> Transition:
    """Replace legs[j:] by the next good connection from `station` (7.4). `over_cap` is the
    actual route of the boarded train legs[j - 1], followed when nothing good fits (6.3).
    `since`: only an answer asked from `station` at or after it proves there is no connection."""
    lock = s.lock
    assert lock is not None
    p, d = ctx.profile, ctx.direction
    kind, sev, text, train = cause
    usual = remaining_pattern(p.journey(d).transfers, station)
    c, how = choose_replacement(pool, usual_remaining=usual, **_good_kw(s, ctx, station, kept, j))
    if c is None and over_cap is not None:
        end = over_cap.legs[0].alight.code
        ride = replace(legs[j - 1], served_endpoint=end, alight_stop=end)
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
        if any(q.origin == station and (since is None or q.at >= since) for q in asked):
            nothing = "no connection found, check the NS app."
            events.append(_event(ctx, EventKind.NO_CONNECTION, Severity.LOUD, nothing))
        return Transition(_progress(s, legs), tuple(events))
    via = ", ".join(c.transfer_stations) or "a direct train"
    text += f". Next good connection: {_hhmm(_departure(c))}" + (
        f", new route via {via}." if how == "new_route" else "."
    )
    written = _write(s, _journey(lock, legs[:j], c.legs, LockSource.RELOCK))
    if history is not None and history.train not in {y.train for y in s.cancellation_history}:
        written = replace(written, cancellation_history=(*s.cancellation_history, history))
    return Transition(written, (_event(ctx, kind, sev, text, train),))


def _follow(
    s: OccurrenceState, ctx: Ctx, pool: ProviderAnswer, asked: tuple[Query, ...]
) -> Transition:
    lock = s.lock
    assert lock is not None
    p, d = ctx.profile, ctx.direction
    dest, now = p.destination(d), ctx.now
    legs = list(lock.legs)
    k = _first_open(lock)
    obs = [_observe(pool, x)[0] for x in legs]
    # a served-endpoint recovery is not the planned pattern, so it is not a route change
    plain = all(x.alight_stop == x.intended_alight for x in legs[k:])
    expected = tuple(x.alight_stop for x in legs[k:-1])
    rc = route_changed([x.train for x in legs[k:]], pool, expected) if plain else None
    events: list[AlertEvent] = []

    def with_events(t: Transition) -> Transition:
        return Transition(t.state, (*events, *t.events))

    def over(j: int) -> Connection | None:
        """The actual route of the boarded kept leg legs[j - 1], if NS reroutes it."""
        boarded = j > 0 and _may_be_on(legs[j - 1], now)
        return rc if rc is not None and boarded and rc.legs[0].train == legs[j - 1].train else None

    for i in range(k, len(legs)):
        x, cl = legs[i], obs[i]
        if cl is None:  # missing or uncertain: reported, never relocked on
            continue
        nxt = legs[i + 1] if i + 1 < len(legs) else None
        if cl.kind is LegKind.CANCELLED and x.state is not LegState.RIDING:
            text = f"your {_name(x)} train is cancelled"
            kept = obs[i - 1] if i else None
            cause = (EventKind.CANCELLED, Severity.LOUD, text, x.train)
            return with_events(
                _relock(s, ctx, pool, asked, legs, i, x.board_station, kept, cause, history=x)
            )
        if cl.kind is LegKind.CUT_SHORT or x.alight_stop != x.intended_alight:
            last = cl.last_served
            end = (
                last.code
                if last is not None
                else x.intended_alight
                if cl.kind in _RUNS
                else x.served_endpoint
            )
            frozen = x.alight_frozen or (nxt is not None and _may_be_on(nxt, now))
            x = legs[i] = replace(
                x,
                served_endpoint=end,
                alight_frozen=frozen,
                alight_stop=x.alight_stop if frozen else end,
            )
            if not frozen:
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
                    return with_events(
                        Transition(_write(s, new), (_event(ctx, kind, sev, f"{text}.", train),))
                    )
                if (nxt is None and end != dest) or (nxt is not None and nxt.board_station != end):
                    original = (
                        nxt if nxt is not None and nxt.board_station == x.intended_alight else None
                    )
                    return with_events(
                        _relock(
                            s, ctx, pool, asked, legs, i + 1, end, cl, cause, original, over(i + 1)
                        )
                    )
        ncl = obs[i + 1] if nxt is not None else None
        if nxt is None or ncl is None or ncl.kind is LegKind.CANCELLED:
            continue  # a cancelled next leg is handled as a cancellation on its own turn
        tc = check_transfer(cl, ncl, x.alight_stop, p.transfer_min)
        asked_late = _late_onward(x, nxt)  # what this poll's queries asked for
        dep = ncl.leg.board.departure
        dep_at = None if dep is None else dep.value or dep.planned
        if tc.margin_min is not None and dep_at is not None:
            # margin is out departure - in arrival, floored: this errs a minute early, the safe way
            onward = dep_at + timedelta(minutes=tc.minimum - tc.margin_min)
            x = legs[i] = replace(x, onward_from=onward)
        if tc.state is TransferState.BROKEN and not _may_be_on(nxt, now):
            text = f"your change at {x.alight_stop} is broken: {tc.reason}"
            cause = (EventKind.TRANSFER_BROKEN, Severity.LOUD, text, nxt.train)
            # "no connection" only once the station was asked after the expected arrival; the
            # query from the last poll's time counts, so a drifting delay does not keep it quiet
            late = _late_onward(x, nxt)
            since = late if late is None or asked_late is None else min(late, asked_late)
            return with_events(
                _relock(
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
                    since,
                )
            )
        if tc.state is TransferState.AT_RISK:
            soon = (
                nxt.train.planned_departure.astimezone(UTC) - now.astimezone(UTC) <= _AT_RISK_LOUD
            )
            text = f"your change at {x.alight_stop} is at risk: {tc.reason}."
            sev = Severity.LOUD if soon else Severity.SILENT
            events.append(_event(ctx, EventKind.TRANSFER_AT_RISK, sev, text, nxt.train))
    if rc is not None and obs[k] is not None:
        x = legs[k]
        via = ", ".join(rc.transfer_stations) or "a direct train"
        text = f"NS now routes your {_name(x)} train via {via}"
        cause = (EventKind.ROUTE_CHANGED, Severity.LOUD, text, x.train)
        kept = obs[k - 1] if k else None
        if not _may_be_on(x, now):
            # 6.3: follow the new route only within the cap and if good, else relock
            if is_good(rc, pool, **_good_kw(s, ctx, x.board_station, kept, k)).ok:
                new = _journey(lock, legs[:k], rc.legs, LockSource.RELOCK)
                note = _event(
                    ctx, EventKind.ROUTE_CHANGED, Severity.LOUD, f"{text}, following it.", x.train
                )
                return with_events(Transition(_write(s, new), (note,)))
            return with_events(_relock(s, ctx, pool, asked, legs, k, x.board_station, kept, cause))
        # a boarded train is never dropped: relock what follows from where NS changes
        end = rc.legs[0].alight.code
        legs[k] = replace(x, served_endpoint=end, alight_stop=end)
        return with_events(_relock(s, ctx, pool, asked, legs, k + 1, end, obs[k], cause, None, rc))
    return Transition(_progress(s, legs), tuple(events))


def on_poll(s: OccurrenceState, ctx: Ctx, board: Board) -> Transition:
    """Apply one poll (7.4, rule 2). A result started under another generation is discarded
    (rule 21). One lock change per poll; the next poll, at the new generation, sees the rest.
    Ongoing facts (`missing`, `transfer_at_risk`, retained ones keyed `...:retained:<n>`) repeat
    every poll with a stable key: the news ledger (7.5) must drop repeats by key."""
    if board.started_generation != s.generation:
        return Transition(s, ())
    s = replace(s, poll_records=(*s.poll_records, *board.polls)[-_KEEP_POLLS:])
    if s.lock is None or s.not_travelling or not board.answers:
        return Transition(s, ())  # before a lock, auto_lock judges the usual
    pool = _pool(board.answers)
    events: list[AlertEvent] = []
    for j in (s.lock, *s.retained):
        on = (
            "" if j is s.lock else f"if you are on the {_hhmm(j.legs[0].train.planned_departure)}: "
        )
        for x in j.legs:
            if _observed(x, ctx.now) and _observe(pool, x)[1] is Observation.MISSING:
                text = f"{on}your {_name(x)} train is not in NS data."
                events.append(_event(ctx, EventKind.MISSING, Severity.LOUD, text, x.train))
    asked = tuple(a.query for a in board.answers)
    t = _restore(s, ctx, pool) or _follow(s, ctx, pool, asked)
    for r in s.retained:
        # rule 7: a retained remainder gets the locked-journey checks but is never relocked, so
        # only what they say is kept, with the next good connection from where it breaks
        first = r.legs[0].train
        on = f"if you are on the {_hhmm(first.planned_departure)}: "
        alone = replace(s, lock=r, retained=(), cancellation_history=())
        for e in _follow(alone, ctx, pool, asked).events:
            head, _, body = e.text.partition(": ")
            key = f"{e.key}:retained:{first.train_number}"
            events.append(replace(e, text=f"{head}: {on}{body}", key=key))
    return Transition(t.state, (*events, *t.events))

"""Occurrence state, the lock writer and user actions (7.2, 7.8, 7.12 rules 1, 10, 11, 21, 7.13).
Pure: `now` always comes from the caller."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum

from train_commute_companion.board import (
    Catchability,
    GoodCheck,
    catch_from,
    choose_replacement,
    is_good,
    max_transfers,
)
from train_commute_companion.calendar import TZ
from train_commute_companion.classify import classify_leg
from train_commute_companion.health import PollRecord
from train_commute_companion.match import usual_connection
from train_commute_companion.model import (
    AdviceRecord,
    AlertEvent,
    Connection,
    DatedSchedule,
    Direction,
    EventKind,
    Leg,
    OccurrenceKey,
    Profile,
    Severity,
    Signature,
    TrainId,
)
from train_commute_companion.providers.base import ProviderAnswer, Query


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
    cancellation_history: tuple[TrainId, ...]
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

# a train counts as one the user may be on once it has departed and is not done (7.12 rule 7);
# callers keep leg states current, so the writer needs no clock
_MAY_BE_ON = (LegState.RIDING, LegState.MISSED)
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


def _rides(j: Journey | None, train: TrainId) -> bool:
    """The user may be on `train` as the first unfinished leg of `j`."""
    first = None if j is None else _unfinished(j)
    return first is not None and first.train == train and first.state in _MAY_BE_ON


def write_lock(
    s: OccurrenceState, new: Journey, *, expect: Journey | None
) -> OccurrenceState | None:
    """Compare-and-set lock write. None when refused (stale `expect` or precedence)."""
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
        if replaced and old.legs[replaced[0]].state in _MAY_BE_ON:
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
    usual = usual_connection(
        answer, p.origin(d), ctx.schedule.first_departure, p.journey(d).transfers, ctx.signature
    )
    events: list[AlertEvent] = []
    if usual is not None:
        locked = write_lock(s, journey_of(usual, LockSource.AUTO, Commitment.INTENDED), expect=None)
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
    locked = write_lock(s, journey_of(nxt, LockSource.AUTO, Commitment.INTENDED), expect=None)
    assert locked is not None
    return Transition(locked, tuple(events))


def _ridden_source(s: OccurrenceState, train: TrainId) -> Journey | None:
    return next((j for j in (s.lock, *s.retained) if j is not None and _rides(j, train)), None)


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
        riding = next((x for x in open_ if _ridden_source(x, first) is not None), None)
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
    source = _ridden_source(s, c.legs[0].train)
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
    written = write_lock(replace(s, retained=()), new, expect=s.lock)
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

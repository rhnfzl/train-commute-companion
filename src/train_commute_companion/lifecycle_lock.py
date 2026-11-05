"""The lock writer (7.2, 7.12 rules 1, 3, 7). Pure: `now` always comes from the caller."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from train_commute_companion.model import (
    Connection,
    DirectionState,
    Leg,
    LegState,
    TrainId,
)
from train_commute_companion.state import (
    Commitment,
    Journey,
    LegProgress,
    LockSource,
    OccurrenceState,
)

# a train the user may be on: departed and not done (7.12 rule 7). RIDING and MISSED say so by
# state; a PENDING or BOARDING leg says so once its planned departure has passed, since leg
# progress may lag the clock (a departed usual is auto-locked PENDING). A delayed train still at
# the platform also counts: retaining it errs on the safe side.
_MAY_BE_ON = (LegState.RIDING, LegState.MISSED)
MAY_HAVE_LEFT = (LegState.PENDING, LegState.BOARDING)
_YIELDS_TO_CHOICE = (LockSource.AUTO, LockSource.STATION, LockSource.LOCATION)
_NEEDS_EXPECT_OVER_CHOICE = (LockSource.RELOCK, LockSource.RESTORE)


def progress_of(leg: Leg) -> LegProgress:
    a = leg.alight.code
    return LegProgress(
        train=leg.train,
        board_station=leg.board.code,
        intended_alight=a,
        served_endpoint=a,
        alight_stop=a,
        alight_frozen=False,
        state=LegState.PENDING,
        accepted_at=None,
        accept_value=None,
        accept_first_seen=None,
        watch_until=None,
    )


def journey_of(c: Connection, source: LockSource, commitment: Commitment) -> Journey:
    last = c.legs[-1]
    arrival = last.alight.planned_arrival or last.train.planned_departure
    return Journey(tuple(progress_of(x) for x in c.legs), source, commitment, arrival)


def unfinished_leg(j: Journey) -> LegProgress | None:
    return next((x for x in j.legs if x.state is not LegState.DONE), None)


def may_be_on(x: LegProgress, now: datetime | None) -> bool:
    if x.state in _MAY_BE_ON:
        return True
    left = now is not None and x.train.planned_departure.astimezone(UTC) <= now.astimezone(UTC)
    return left and x.state in MAY_HAVE_LEFT


def rides(j: Journey | None, train: TrainId, now: datetime | None) -> bool:
    """The user may be on `train` as the first unfinished leg of `j`."""
    first = None if j is None else unfinished_leg(j)
    return first is not None and first.train == train and may_be_on(first, now)


def _lock_refused(old: Journey | None, new: Journey, expect: Journey | None) -> bool:
    if expect is not None and old != expect:
        return True
    if old is None or old.source is not LockSource.CHOOSE:
        return False
    return new.source in _YIELDS_TO_CHOICE or (
        new.source in _NEEDS_EXPECT_OVER_CHOICE and expect is None
    )


def _merged_legs(old: Journey | None, new: Journey) -> tuple[LegProgress, ...]:
    """Completed travel stays; a caller passing a done leg again may update its watched fields
    (served endpoint, alighting stop, acceptance). A choice brings its own prefix (7.12 r7):
    completed legs of another journey are never merged into it."""
    newer = {x.train: x for x in new.legs}
    done = (
        ()
        if old is None or new.source is LockSource.CHOOSE
        else tuple(newer.get(x.train, x) for x in old.legs if x.state is LegState.DONE)
    )
    done_trains = {x.train for x in done}
    return done + tuple(x for x in new.legs if x.train not in done_trains)


def _ridden_remainder(
    old: Journey, legs: Sequence[LegProgress], now: datetime | None
) -> tuple[Journey, ...]:
    kept = {x.train for x in legs}
    replaced = [
        i for i, x in enumerate(old.legs) if x.state is not LegState.DONE and x.train not in kept
    ]
    # from the first replaced train the user may be on: an earlier replaced one (say a
    # cancelled train never boarded) does not hide a later one that may be ridden
    on = next((i for i in replaced if may_be_on(old.legs[i], now)), None)
    return () if on is None else (replace(old, legs=old.legs[on:]),)


def write_lock(
    s: OccurrenceState, new: Journey, *, expect: Journey | None, now: datetime | None
) -> OccurrenceState | None:
    """Compare-and-set lock write. None when refused (stale `expect` or precedence).
    `now` decides whether a replaced train has departed, so it is retained (7.12 rule 7); None
    judges by leg state only, for a caller that already knows the replaced train is not ridden."""
    old = s.lock
    if _lock_refused(old, new, expect):
        return None
    legs = _merged_legs(old, new)
    retained = s.retained
    if old is not None:
        retained += _ridden_remainder(old, legs, now)
    state = s.direction_state
    return replace(
        s,
        lock=replace(new, legs=legs),
        retained=retained,
        generation=s.generation + 1,
        direction_state=DirectionState.LOCKED if state is DirectionState.IDLE else state,
        errand_deadline=s.errand_deadline if new.commitment is Commitment.PROVISIONAL else None,
    )

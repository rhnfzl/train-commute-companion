"""Lifecycle edge cases about cancellation, relock and restore, generalised to journeys."""

from dataclasses import replace

from builders import make_leg
from test_lifecycle_follow import (
    L1,
    L1X,
    L2,
    L2X,
    LATER,
    R2,
    RIDE,
    ans,
    at,
    conn,
    ctx,
    journey,
    kinds,
    numbers,
    occ,
    poll,
    usual_locked,
)

from train_commute_companion.lifecycle import LegState, LockSource
from train_commute_companion.model import EventKind, Severity


def test_cancellation_relock_moves_even_a_chosen_journey():
    s = occ(journey([L1, L2], source=LockSource.CHOOSE))
    t = poll(s, ctx(at("07:50")), ans("AAA", conn(L1X, L2), LATER))
    assert numbers(t.state) == ["1300", "6666"]
    assert t.state.lock is not None and t.state.lock.source is LockSource.RELOCK


def test_cancellation_already_alerted_still_moves_the_lock_once_a_train_runs():
    s = usual_locked()
    first = poll(s, ctx(at("07:50")), ans("AAA", conn(L1X, L2)))
    assert EventKind.NO_CONNECTION in [e.kind for e in first.events]
    assert first.state.lock == s.lock
    t = poll(first.state, ctx(at("07:52")), ans("AAA", conn(L1X, L2), LATER))
    assert numbers(t.state) == ["1300", "6666"]


def test_cancellation_after_planned_arrival_still_alerts_and_moves_the_lock():
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=15)  # 08:55 at UT
    s = occ(journey([L1, L2], RIDE))
    t = poll(s, ctx(at("09:00")), ans("AAA", conn(late, L2X)), ans("UT", conn(L2X), conn(R2)))
    assert kinds(t) == [(EventKind.CANCELLED, Severity.LOUD)]
    assert numbers(t.state) == ["1234", "6666"]
    assert t.state.retained == ()  # a cancelled train is never a train the user may be on


def test_relock_target_inside_the_buffer_needs_the_platform():
    soon = conn(make_leg("AAA", "UT", "08:05", "08:33", number="1100"), L2)
    answer = ans("AAA", conn(L1X, L2), soon, LATER)
    assert numbers(poll(usual_locked(), ctx(at("08:00")), answer).state) == ["1300", "6666"]
    on_platform = replace(usual_locked(), at_station=True)
    assert numbers(poll(on_platform, ctx(at("08:00")), answer).state) == ["1100", "5678"]


def test_reinstated_original_after_a_cancellation_relock_moves_back():
    relocked = poll(usual_locked(), ctx(at("07:50")), ans("AAA", conn(L1X, L2), LATER)).state
    still = poll(relocked, ctx(at("07:52")), ans("AAA", conn(L1X, L2), LATER))
    assert numbers(still.state) == ["1300", "6666"]  # still cancelled: nothing moves
    back = poll(still.state, ctx(at("07:54")), ans("AAA", conn(L1, L2), LATER))
    assert numbers(back.state) == ["1234", "5678"] and "back to normal" in back.events[0].text


def test_chosen_future_train_missing_from_the_answer_is_not_substituted():
    s = occ(journey([L1, L2], source=LockSource.CHOOSE))
    t = poll(s, ctx(at("07:50")), ans("AAA", LATER))
    assert kinds(t) == [(EventKind.MISSING, Severity.LOUD)] * 2
    assert t.state.lock == s.lock
    assert t.state.lock is not None and t.state.lock.legs[0].state is LegState.PENDING

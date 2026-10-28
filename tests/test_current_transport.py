"""6.3: a train whose current copy is other transport is never the usual journey, a reroute,
the ride advice compares with, nor "on time" (no false reassurance)."""

from dataclasses import replace

from builders import make_leg
from test_lifecycle_follow import (
    L1,
    DirectionState,
    at,
    board,
    conn,
    ctx,
    journey,
    numbers,
    occ,
    poll,
)

from train_commute_companion.advice import _ridden
from train_commute_companion.lifecycle import _pool, auto_lock
from train_commute_companion.match import route_changed, usual_connection
from train_commute_companion.news import all_good
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.serde import decode, encode

A2 = make_leg("UT", "RTD", "08:50", "09:24", number="5678")
A2_LATE = make_leg("UT", "RTD", "08:50", "09:24", number="5678", delay_arr=30)  # now 09:54
B2 = make_leg("UT", "RTD", "08:55", "09:33", number="6666")
B2_BUS = replace(B2, alternative_transport=True, category="BUS")
A, B = conn(L1, A2), conn(L1, B2)


def answer(origin, when, *conns):
    q = Query(origin, "RTD", at(when))
    return ProviderAnswer(q, conns, q.at)


def pool():
    # the origin answer still shows B as a train; the newer transfer answer shows the bus
    return _pool([answer("AAA", "08:00", A, B), answer("UT", "08:01", conn(A2_LATE), conn(B2_BUS))])


def test_usual_connection_skips_a_train_that_is_now_a_bus():
    p = pool()
    assert usual_connection(p, "AAA", at("08:12"), ("UT",), None) is A
    s = occ(None, direction_state=DirectionState.IDLE)
    assert _ridden(s, ctx(at("07:50")), p) is A


def test_auto_lock_never_locks_a_bus_and_keeps_its_lock_across_a_restart():
    s = occ(None, direction_state=DirectionState.IDLE)
    t = auto_lock(s, ctx(at("07:50")), pool())
    assert numbers(t.state) == ["1234", "5678"]
    back = decode(type(t.state), encode(t.state))
    t2 = poll(back, ctx(at("07:52")), *pool().answers)
    assert numbers(t2.state) == ["1234", "5678"]


def test_route_change_never_picks_a_bus():
    x = make_leg("BBB", "RTD", "08:40", "09:10", number="7777")  # arrives first
    bus_way = conn(make_leg("AAA", "BBB", "08:12", "08:30", number="1234"), x)
    train_way = conn(make_leg("AAA", "ZZZ", "08:12", "08:35", number="1234"), A2)
    older = answer("AAA", "08:00", bus_way, train_way)
    newer = answer("BBB", "08:01", conn(replace(x, alternative_transport=True)))
    assert route_changed([L1.train], older, ("UT",)) is bus_way  # still a train
    assert route_changed([L1.train], _pool([older, newer]), ("UT",)) is train_way


def test_no_all_good_for_a_locked_train_any_answer_shows_as_a_bus():
    s = occ(journey([L1, B2]))
    good = replace(board(s), answers=(answer("AAA", "08:00", B), answer("AAA", "08:01", B)))
    assert all_good(s, good, ctx(at("08:02"))) is not None
    bad = replace(
        good, answers=(answer("AAA", "08:00", B), answer("AAA", "08:01", conn(L1, B2_BUS)))
    )
    assert all_good(s, bad, ctx(at("08:02"))) is None

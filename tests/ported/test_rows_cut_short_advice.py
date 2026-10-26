"""Board rows 9, 11 and 17, lifecycle row 48: cut-short trains and advice that stops."""

from dataclasses import replace

from builders import make_connection, make_leg
from test_advice import EARLY, LATER, USUAL_ARRIVAL, answer, at, conn, ctx, fresh
from test_lifecycle_follow import DIRECT, T_BBB, ans, direct_riding, kinds, poll
from test_lifecycle_follow import at as out_at
from test_lifecycle_follow import ctx as out_ctx

from train_commute_companion.advice import advise
from train_commute_companion.model import EventKind, Severity


def cut_short(onward_arrival: str | None):
    """The usual 17:52 train cut short at BBB, with an optional onward train to AAA."""
    part = make_leg("UT", "AAA", "17:52", "18:26", number="1234", stops=["BBB"], cut_after="BBB")
    if onward_arrival is None:
        return make_connection([part])
    return make_connection([part, make_leg("BBB", "AAA", "18:10", onward_arrival, number="3000")])


def test_cut_short_with_a_connection_22_min_late_triggers_advice():
    c = ctx(at("17:00"))
    _, events = advise(fresh(c), c, answer(EARLY, cut_short("18:48"), LATER), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]


def test_cut_short_without_a_connection_advises_without_calling_it_cancelled():
    c = ctx(at("17:00"))
    _, events = advise(fresh(c), c, answer(EARLY, cut_short(None), LATER), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    assert "cancel" not in events[0].text.lower()


def test_cut_short_with_no_listed_connection_keeps_the_lock():
    c = out_ctx(out_at("08:14"), DIRECT)
    s = direct_riding()
    t = poll(s, c, ans("AAA", make_connection([T_BBB])))
    assert kinds(t) == [(EventKind.CUT_SHORT, Severity.LOUD)]
    assert "runs only to BBB, change there" in t.events[0].text
    assert t.state.lock is not None and s.lock is not None
    assert t.state.lock.legs[0].train == s.lock.legs[0].train


def test_earlier_train_advice_stops_quietly_while_the_usual_train_is_still_cancelled():
    c = ctx(at("17:00"))
    cancelled = conn("17:52", "18:26", "1234", cancelled=True)
    rec, first = advise(fresh(c), c, answer(EARLY, cancelled, LATER), USUAL_ARRIVAL)
    assert [e.kind for e in first] == [EventKind.LEAVE_NOW]
    # the earlier train is gone, the usual one is still cancelled: nothing to withdraw
    s = replace(fresh(c), advice=rec)
    _, events = advise(s, ctx(at("17:10")), answer(cancelled, LATER), USUAL_ARRIVAL)
    assert all(e.kind is not EventKind.EARLIER_TRAIN_WITHDRAWN for e in events)

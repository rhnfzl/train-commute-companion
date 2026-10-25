"""Board rows 58-63 as ported to whole journeys."""

from test_advice import EARLY, LATER, SLOW, USUAL_ARRIVAL, answer, at, conn, ctx, fresh

from train_commute_companion.advice import advise
from train_commute_companion.model import EventKind


def test_picks_the_largest_saving_and_a_tie_goes_to_the_earlier_train():
    c = ctx(at("17:00"))
    a = conn("17:10", "17:50", "1")
    b = conn("17:20", "17:50", "2")  # same arrival: tie goes to the earlier train
    d = conn("17:30", "17:58", "3")
    rec, events = advise(fresh(c), c, answer(d, b, a, SLOW), USUAL_ARRIVAL)
    assert [e.kind for e in events] == [EventKind.LEAVE_NOW]
    assert rec.advised_train == a.legs[0].train


def test_never_suggested_when_nothing_is_bad():
    c = ctx(at("17:00"))
    _, events = advise(
        fresh(c), c, answer(EARLY, conn("17:52", "18:30", "1234"), LATER), USUAL_ARRIVAL
    )
    assert events == []  # 4 min late is under the 10 min threshold


def test_a_cancelled_earlier_candidate_is_not_advised():
    c = ctx(at("17:00"))
    dead = conn("17:22", "17:56", "1000", cancelled=True)
    _, events = advise(fresh(c), c, answer(dead, SLOW), USUAL_ARRIVAL)
    assert events == []

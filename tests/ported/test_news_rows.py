"""News rules by behaviour: what changes the alert key and what does not."""

from builders import make_leg
from test_news import key

from train_commute_companion.model import Provenance


def leg(**k):
    return make_leg("AAA", "UT", "08:12", "08:40", number="1234", **k)


def test_same_facts_give_the_same_key():
    assert key(leg()) == key(leg())


def test_departure_or_arrival_moving_three_minutes_is_news():
    assert key(leg(delay_dep=3, delay_arr=3)) != key(leg(delay_dep=6, delay_arr=6))


def test_cancelled_train_differs_from_running():
    assert key(leg(cancelled=True)) != key(leg())


def test_cut_short_stop_change_is_news():
    a = make_leg(
        "AAA", "UT", "08:12", "08:40", number="1234", stops=["BBB", "XXX"], cut_after="BBB"
    )
    b = make_leg(
        "AAA", "UT", "08:12", "08:40", number="1234", stops=["BBB", "XXX"], cut_after="XXX"
    )
    assert key(a) != key(b)


def test_planned_only_time_does_not_change_key_by_itself():
    assert key(leg(provenance=Provenance.PLANNED)) == key(leg())

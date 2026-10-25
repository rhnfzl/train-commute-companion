from builders import make_connection, make_leg

from train_commute_companion.classify import classify_leg, find_leg
from train_commute_companion.model import LegKind, Observation, Provenance
from train_commute_companion.providers.base import ProviderAnswer, Query


def test_numeric_string_number_matches_only_exactly():
    leg = make_leg("UT", "RTD", "08:46", "09:24", number="1")
    q = Query("UT", "RTD", leg.train.planned_departure)
    ans = ProviderAnswer(q, (make_connection([leg]),), q.at)
    assert find_leg(ans, leg.train, "UT")[1] is Observation.SEEN
    other = make_leg("UT", "RTD", "08:46", "09:24", number="01").train
    assert find_leg(ans, other, "UT")[1] is Observation.UNCERTAIN


def test_delay_wobble_flips_only_at_three_minutes():
    got = [
        classify_leg(make_leg("UT", "RTD", "08:46", "09:24", delay_dep=d), "RTD").kind
        for d in (2, 3, 2, 3)
    ]
    assert got == [LegKind.OK, LegKind.DELAYED, LegKind.OK, LegKind.DELAYED]


def test_planned_only_times_give_no_delay():
    leg = make_leg("UT", "RTD", "08:46", "09:24", provenance=Provenance.PLANNED)
    cl = classify_leg(leg, "RTD")
    assert cl.kind is LegKind.OK and cl.dep_delay_min == 0


def test_missing_times_give_unknown_delay_not_zero():
    leg = make_leg("UT", "RTD", "08:46", "09:24", provenance=Provenance.MISSING)
    cl = classify_leg(leg, "RTD")
    assert cl.dep_delay_min is None and cl.arr_delay_min is None

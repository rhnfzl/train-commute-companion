from builders import make_connection, make_leg

from train_commute_companion.classify import classify_leg, find_leg
from train_commute_companion.model import LegKind, Observation, TrainId
from train_commute_companion.providers.base import ProviderAnswer, Query


def answer(*legs):
    q = Query("UT", "RTD", legs[0].train.planned_departure if legs else None)  # type: ignore[arg-type]
    return ProviderAnswer(q, tuple(make_connection([leg]) for leg in legs), q.at)


def train(board, hhmm, number):
    leg = make_leg(board, "RTD", hhmm, "23:00", number=number)
    return leg.train


def test_cut_short_is_never_cancelled():
    cl = classify_leg(make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"], cut_after="UT"), "RTD")
    assert cl.kind is LegKind.CUT_SHORT and cl.last_served is not None
    assert cl.last_served.code == "UT"


def test_cut_after_alight_station_is_ok():
    leg = make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"], cut_after="UT")
    assert classify_leg(leg, "UT").kind is LegKind.OK


def test_first_stop_cancelled_is_cancelled():
    leg = make_leg("AAA", "RTD", "08:12", "09:00", cancelled=True)
    cl = classify_leg(leg, "RTD")
    assert cl.kind is LegKind.CANCELLED and cl.last_served is None


def test_delay_3_min_is_delayed_2_is_ok():
    assert (
        classify_leg(make_leg("UT", "RTD", "08:46", "09:24", delay_dep=2), "RTD").kind is LegKind.OK
    )
    cl = classify_leg(make_leg("UT", "RTD", "08:46", "09:24", delay_dep=3), "RTD")
    assert cl.kind is LegKind.DELAYED and cl.dep_delay_min == 3


def test_arrival_delay_alone_delays():
    assert (
        classify_leg(make_leg("UT", "RTD", "08:46", "09:24", delay_arr=4), "RTD").kind
        is LegKind.DELAYED
    )


def test_two_trains_same_minute_is_uncertain():
    ans = answer(
        make_leg("UT", "RTD", "08:46", "09:24", number="1"),
        make_leg("UT", "RTD", "08:46", "09:30", number="2"),
    )
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.UNCERTAIN)


def test_same_minute_other_number_is_uncertain():
    ans = answer(make_leg("UT", "RTD", "08:46", "09:24", number="2"))
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.UNCERTAIN)


def test_exact_match_is_seen_even_when_listed_twice():
    leg = make_leg("UT", "RTD", "08:46", "09:24", number="1")
    found, obs = find_leg(answer(leg, leg), train("UT", "08:46", "1"), "UT")
    assert obs is Observation.SEEN and found == leg


def test_absent_is_missing():
    ans = answer(make_leg("UT", "RTD", "08:50", "09:24", number="1"))
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.MISSING)


def test_empty_answer_is_missing():
    q = Query("UT", "RTD", train("UT", "08:46", "1").planned_departure)
    ans = ProviderAnswer(q, (), q.at)
    assert find_leg(ans, train("UT", "08:46", "1"), "UT") == (None, Observation.MISSING)


def test_other_board_station_does_not_match():
    ans = answer(make_leg("AAA", "RTD", "08:46", "09:24", number="1"))
    assert find_leg(ans, train("UT", "08:46", "1"), "UT")[1] is Observation.MISSING


def test_other_operating_date_is_uncertain():
    other = make_leg("UT", "RTD", "08:46", "09:24", number="1")
    want = TrainId(other.train.operating_date.replace(day=26), "1", other.train.planned_departure)
    assert find_leg(answer(other), want, "UT")[1] is Observation.UNCERTAIN

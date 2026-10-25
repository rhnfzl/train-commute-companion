from builders import make_leg

from train_commute_companion.classify import classify_leg
from train_commute_companion.model import LegKind


def kinds(*legs):
    return [classify_leg(leg, "RTD").kind for leg in legs]


def test_mixed_board_classifies_each_row_by_its_own_stops():
    assert kinds(
        make_leg("UT", "RTD", "08:12", "09:00", stops=["XXX"], cut_after="XXX"),
        make_leg("UT", "RTD", "08:20", "09:08", cancelled=True),
        make_leg("UT", "RTD", "08:30", "09:18", delay_dep=2),
        make_leg("UT", "RTD", "08:40", "09:28", delay_dep=4),
    ) == [LegKind.CUT_SHORT, LegKind.CANCELLED, LegKind.OK, LegKind.DELAYED]


def test_cancelled_flag_without_cancelled_stops_is_not_cancelled():
    from dataclasses import replace

    leg = replace(make_leg("UT", "RTD", "08:12", "09:00"), cancelled=True)
    assert classify_leg(leg, "RTD").kind is LegKind.OK


def test_cut_short_before_any_served_stop_is_cancelled():
    leg = make_leg("UT", "RTD", "08:12", "09:00", cut_after="UT")
    assert classify_leg(leg, "RTD").kind is LegKind.CANCELLED


def test_cancelled_train_expected_time_is_planned():
    cl = classify_leg(make_leg("UT", "RTD", "08:12", "09:00", cancelled=True, delay_dep=5), "RTD")
    assert cl.dep_delay_min == 0 and cl.arr_delay_min is None


def test_cut_short_has_no_arrival_delay_but_keeps_departure_delay():
    cl = classify_leg(
        make_leg("UT", "RTD", "08:12", "09:00", stops=["XXX"], cut_after="XXX", delay_dep=22), "RTD"
    )
    assert cl.kind is LegKind.CUT_SHORT and cl.dep_delay_min == 22 and cl.arr_delay_min is None


def test_late_arrival_on_time_departure_is_delayed():
    cl = classify_leg(make_leg("UT", "RTD", "08:12", "09:00", delay_arr=8), "RTD")
    assert cl.kind is LegKind.DELAYED and cl.arr_delay_min == 8 and cl.dep_delay_min == 0

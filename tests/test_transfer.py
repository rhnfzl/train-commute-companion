from dataclasses import replace

from builders import make_leg

from train_commute_companion.classify import classify_leg
from train_commute_companion.model import Observation, Provenance, TransferState
from train_commute_companion.transfer import check_transfer, transfer_minimum

PM = {"UT": 4}


def cl(*a, obs=Observation.SEEN, **k):
    leg = make_leg(*a, **k)
    return replace(classify_leg(leg, leg.alight.code), observation=obs)


def test_minimum_is_max_of_user_and_provider():
    assert transfer_minimum("UT", {"UT": 4}, 2, 3) == 4
    assert transfer_minimum("UT", {"UT": 2}, 2, 3) == 3


def test_unknown_station_fallbacks():
    assert transfer_minimum("XXX", {"UT": 4}, None, None) == 4
    assert transfer_minimum("XXX", {}, None, None) == 5
    assert transfer_minimum("XXX", {"UT": 4}, 2, None) == 2


def test_broken_by_delay():
    tc = check_transfer(
        cl("AAA", "UT", "08:12", "08:40", delay_arr=5), cl("UT", "RTD", "08:46", "09:24"), "UT", PM
    )
    assert tc.state is TransferState.BROKEN and tc.margin_min == 1


def test_ok_and_at_risk_below_min_plus_2():
    out = cl("UT", "RTD", "08:46", "09:24")
    ok = check_transfer(cl("AAA", "UT", "08:12", "08:40"), out, "UT", PM)
    assert ok.state is TransferState.OK and ok.margin_min == 6
    risk = check_transfer(cl("AAA", "UT", "08:12", "08:40", delay_arr=1), out, "UT", PM)
    assert risk.state is TransferState.AT_RISK and risk.margin_min == 5
    edge = check_transfer(cl("AAA", "UT", "08:12", "08:42"), out, "UT", PM)
    assert edge.state is TransferState.AT_RISK and edge.margin_min == 4


def test_planned_only_times_make_transfer_at_risk():
    inc = cl("AAA", "UT", "08:12", "08:30", provenance=Provenance.PLANNED)
    tc = check_transfer(inc, cl("UT", "RTD", "08:46", "09:24"), "UT", PM)
    assert tc.state is TransferState.AT_RISK and tc.margin_min == 16


def test_missing_time_makes_transfer_at_risk():
    out = cl("UT", "RTD", "08:46", "09:24", provenance=Provenance.MISSING)
    tc = check_transfer(cl("AAA", "UT", "08:12", "08:30"), out, "UT", PM)
    assert tc.state is TransferState.AT_RISK


def test_uncertain_leg_makes_transfer_at_risk():
    inc = cl("AAA", "UT", "08:12", "08:30", obs=Observation.UNCERTAIN)
    assert check_transfer(inc, cl("UT", "RTD", "08:46", "09:24"), "UT", PM).state is (
        TransferState.AT_RISK
    )


def test_cancelled_second_leg_breaks_transfer():
    out = cl("UT", "RTD", "08:46", "09:24", cancelled=True)
    tc = check_transfer(cl("AAA", "UT", "08:12", "08:30"), out, "UT", PM)
    assert tc.state is TransferState.BROKEN


def test_incoming_cut_short_before_station_breaks_transfer():
    inc = cl("AAA", "UT", "08:12", "08:40", stops=["BBB"], cut_after="BBB")
    assert check_transfer(inc, cl("UT", "RTD", "08:46", "09:24"), "UT", PM).state is (
        TransferState.BROKEN
    )


def test_provider_flags_break_transfer():
    inc = cl("AAA", "UT", "08:12", "08:30")
    inc = replace(inc, leg=replace(inc.leg, change_possible=False))
    assert check_transfer(inc, cl("UT", "RTD", "08:46", "09:24"), "UT", PM).state is (
        TransferState.BROKEN
    )


def test_cross_platform_never_lowers_minimum():
    inc = cl("AAA", "UT", "08:12", "08:40", delay_arr=3)
    inc = replace(inc, leg=replace(inc.leg, cross_platform=True))
    tc = check_transfer(inc, cl("UT", "RTD", "08:46", "09:24"), "UT", PM)
    assert tc.minimum == 4 and tc.margin_min == 3 and tc.state is TransferState.BROKEN

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from builders import AMS, make_leg

from train_commute_companion.classify import classify_leg
from train_commute_companion.model import LegKind, Observation, Provenance, TransferState
from train_commute_companion.providers.base import Query
from train_commute_companion.providers.ns import map_trips
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


def utc_margin_case(day, inc_arr, out_dep, *, fold_arr=0, fold_dep=0):
    inc = cl("AAA", "UT", "01:20", inc_arr, day=day, fold_arr=fold_arr)
    out = cl("UT", "RTD", out_dep, "03:40", day=day, fold_dep=fold_dep, number="2222")
    return check_transfer(inc, out, "UT", PM)


def test_dst_repeated_hour_margin_uses_instants():
    # arrive at the second 02:10 (CET), depart at the first 02:15 (CEST): left 55 min earlier
    tc = utc_margin_case(date(2026, 10, 24), "02:10", "02:15", fold_arr=1)
    assert tc.state is TransferState.BROKEN and tc.margin_min == -55


def test_dst_skipped_hour_margin_uses_instants():
    # 01:58 CET to 03:01 CEST is 3 real minutes, not 63
    tc = utc_margin_case(date(2026, 3, 28), "01:58", "03:01")
    assert tc.state is TransferState.BROKEN and tc.margin_min == 3


def test_outgoing_leg_transfer_message_sets_minimum_from_mapped_data():
    raw = (Path(__file__).parent / "data" / "ns" / "one_transfer.json").read_text()
    raw = raw.replace("T08:42", "T08:44").replace("3 min. overstaptijd", "7 min. overstaptijd")
    q = Query("AAA", "BBB", datetime(2026, 10, 27, 8, 0, tzinfo=AMS))
    leg0, leg1 = map_trips(json.loads(raw), q, q.at).connections[0].legs
    assert leg0.transfer_time_to_next == 2 and leg1.transfer_message_min == 7
    a, b = classify_leg(leg0, "UT"), classify_leg(leg1, "BBB")
    tc = check_transfer(a, b, "UT", {"UT": 2})
    assert tc.minimum == 7 and tc.margin_min == 4 and tc.state is TransferState.BROKEN


def test_outgoing_not_reachable_breaks_transfer():
    out = cl("UT", "RTD", "08:46", "09:24")
    out = replace(out, leg=replace(out.leg, reachable=False))
    tc = check_transfer(cl("AAA", "UT", "08:12", "08:30"), out, "UT", PM)
    assert tc.state is TransferState.BROKEN and "reachable" in tc.reason


def test_incoming_must_call_and_outgoing_must_board_at_the_station():
    out = cl("UT", "RTD", "08:46", "09:24")
    elsewhere = cl("AAA", "BBB", "08:12", "08:30")
    assert check_transfer(elsewhere, out, "UT", PM).state is TransferState.BROKEN
    other_board = cl("BBB", "RTD", "08:46", "09:24")
    tc = check_transfer(cl("AAA", "UT", "08:12", "08:30"), other_board, "UT", PM)
    assert tc.state is TransferState.BROKEN and "board" in tc.reason


def test_cut_short_at_the_transfer_station_connects_to_a_recovery_leg():
    leg = make_leg("AAA", "RTD", "08:12", "09:00", stops=["UT"], cut_after="UT")
    inc = classify_leg(leg, "RTD")
    assert inc.kind is LegKind.CUT_SHORT
    tc = check_transfer(inc, cl("UT", "RTD", "08:50", "09:30", number="2222"), "UT", PM)
    assert tc.state is TransferState.OK and tc.margin_min == 38

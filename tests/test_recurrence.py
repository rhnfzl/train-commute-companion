from dataclasses import replace
from datetime import date, time

import pytest
from runtime_rig import DAY, PROFILE, at

from train_commute_companion import overlap
from train_commute_companion.lookahead_probe import probe_dates
from train_commute_companion.model import Config, Direction
from train_commute_companion.overlap import find_overlaps
from train_commute_companion.profile import parse_profiles
from train_commute_companion.recurrence import MonthlyWeekday, RepeatSchedule
from train_commute_companion.schedule import schedules_active

MONDAY = PROFILE.split('  - id: "p2"')[0].replace("[mon, tue, wed, thu, fri]", "[mon]")


def test_monthly_profile_drives_active_windows_and_lookahead():
    text = MONDAY + (
        "    skip_public_holidays: false\n"
        "    repeat: {start_date: 2026-11-01, monthly: 5, end_after: 2}\n"
    )
    profiles, errors = parse_profiles(text, None)
    assert not errors
    (profile,) = profiles
    expected = date(2026, 11, 30), date(2027, 3, 29)
    assert profile.repeat.end == expected[-1]
    assert all(profile.commutes_on(day) for day in expected)
    assert not profile.commutes_on(date(2027, 5, 31))
    assert not profile.commutes_on(date(2027, 2, 22))
    assert probe_dates(profile, date(2026, 11, 29), direction=Direction.OUTBOUND).dates == (
        expected[0],
    )
    config = Config(1, tuple(profiles))
    assert schedules_active(config, {}, at("08:01", expected[0]))
    assert not schedules_active(config, {}, at("08:01", date(2027, 5, 31)))


def test_weekly_count_excludes_configured_public_holidays():
    text = MONDAY.replace("[mon]", "[fri]") + (
        "    repeat: {start_date: 2026-12-18, every_weeks: 1, end_after: 2}\n"
    )
    (profile,), errors = parse_profiles(text, None)
    assert not errors
    assert profile.repeat.end == date(2027, 1, 8)
    assert not profile.commutes_on(date(2026, 12, 25))
    assert not profile.commutes_on(date(2027, 1, 1))


@pytest.mark.parametrize(
    "fields",
    [
        "start_date: 2026-11-01, end_date: 2026-10-01",
        "start_date: 2026-11-01, monthly: 1, every_weeks: 2",
        "start_date: 2026-11-01, end_date: 2027-01-01, end_after: 2",
        "start_date: 2026-11-01, end_after: true",
        "start_date: 2026-11-01, monthly: 6",
        "monthly: 1, end_after: 3",
        "every_weeks: 3, end_after: 3",
    ],
)
def test_invalid_recurrence_fields_are_rejected(fields):
    profiles, errors = parse_profiles(MONDAY + f"    repeat: {{{fields}}}\n", None)
    assert not profiles
    assert errors and all(".repeat" in e.path for e in errors)


def test_profile_overlap_respects_anchors_end_dates_and_distant_matching_phases():
    (profile,), errors = parse_profiles(MONDAY, None)
    assert not errors
    weekly = replace(
        profile,
        repeat=RepeatSchedule(date(2026, 11, 2), date(2028, 11, 1), interval=52),
    )
    monthly = replace(
        profile,
        id="p2",
        name="monthly",
        repeat=RepeatSchedule(date(2026, 11, 1), date(2028, 11, 1), monthly=MonthlyWeekday(5, 1)),
    )
    (collision,) = find_overlaps([weekly, monthly], DAY)
    assert "2028-10-30" in collision.message
    monthly = replace(monthly, repeat=replace(monthly.repeat, end=date(2028, 10, 29)))
    assert not find_overlaps([weekly, monthly], DAY)


@pytest.mark.parametrize(
    ("rule", "commutes", "skips"),
    [
        ("monthly: 1", date(2026, 11, 2), date(2026, 11, 9)),
        ("every_weeks: 3", date(2026, 11, 9), date(2026, 11, 2)),
    ],
)
def test_recurrence_without_start_has_a_stable_calendar_phase(rule, commutes, skips):
    profiles, errors = parse_profiles(MONDAY + f"    repeat: {{{rule}}}\n", None)
    assert not errors
    (profile,) = profiles
    assert profile.commutes_on(commutes)
    assert not profile.commutes_on(skips)


@pytest.mark.parametrize(("skip", "offset"), [("holiday", 0), ("pause", 0), ("pause", 1)])
def test_bounded_overlap_requires_an_active_commute(skip, offset):
    day = date(2026, 12, 25) if skip == "holiday" else date(2026, 11, 6)
    end = f"end_date: {day}" if skip == "holiday" else "end_after: 1"
    text = MONDAY.replace("[mon]", "[fri]") + f"    repeat: {{start_date: {day}, {end}}}\n"
    (p,), errors = parse_profiles(text, None)
    assert not errors
    if skip == "pause":
        p = replace(p, pauses=((day, day.replace(day=day.day + offset)),))
    p = replace(p, return_=replace(p.return_, day_offset=1))
    collisions = find_overlaps([p, replace(p, id="p2", name="other")], DAY)
    assert bool(collisions) == (skip == "pause" and offset == 0)


def test_bounded_overnight_overlap_ignores_paused_service_date():
    (p,), errors = parse_profiles(
        MONDAY + "    repeat: {start_date: 2026-11-02, end_after: 1}\n", None
    )
    assert not errors
    (tuesday,), errors = parse_profiles(
        MONDAY.replace("[mon]", "[tue]") + "    repeat: {start_date: 2026-11-03, end_after: 1}\n",
        None,
    )
    assert not errors
    p = replace(
        p,
        return_=replace(
            p.return_, first_train=time(8, 12), window=(time(7), time(10)), day_offset=1
        ),
        pauses=((date(2026, 11, 3), date(2026, 11, 3)),),
    )
    tuesday = replace(tuesday, id="p2", name="other")
    assert not find_overlaps([p, tuesday], DAY)
    assert find_overlaps([replace(p, pauses=()), tuesday], DAY)


def test_ongoing_overlap_finds_active_dates_after_a_full_calendar_cycle():
    text = MONDAY + "    repeat: {start_date: 2026-11-01, monthly: 1}\n"
    (p,), errors = parse_profiles(text, None)
    assert not errors
    paused = replace(
        p,
        id="p2",
        name="other",
        pauses=((date(2026, 11, 1), date(2427, 1, 31)),),
    )
    (collision,) = find_overlaps([p, paused], DAY)
    assert "2427-02-01" in collision.message


def test_ongoing_profile_self_check_limits_monthly_candidates(monkeypatch):
    text = MONDAY + "    repeat: {start_date: 2026-11-01, monthly: 1}\n"
    (p,), errors = parse_profiles(text, None)
    assert not errors
    p = replace(
        p, return_=replace(p.return_, first_train=time(13, 52), window=(time(12, 52), time(15, 52)))
    )
    original = MonthlyWeekday.dates
    checked = 0

    def counted(rule, start, end):
        nonlocal checked
        for day in original(rule, start, end):
            checked += 1
            yield day

    monkeypatch.setattr(MonthlyWeekday, "dates", counted)
    assert not find_overlaps([p], DAY)
    assert checked <= 4812


def test_weekly_disjoint_windows_skip_the_calendar_scan(monkeypatch):
    text = PROFILE.split('  - id: "p2"')[0] + "    repeat: {every_weeks: 1}\n"
    (p,), errors = parse_profiles(text, None)
    assert not errors
    p = replace(
        p, return_=replace(p.return_, first_train=time(13, 52), window=(time(12, 52), time(15, 52)))
    )

    def unexpected(*args):
        pytest.fail("disjoint weekly windows should be ruled out before the dated scan")

    monkeypatch.setattr(overlap, "_windows", unexpected)
    assert not find_overlaps([p], DAY)


def test_weekly_window_overlap_can_only_occur_in_the_repeated_hour():
    text = MONDAY.replace("[mon]", "[sun]") + "    repeat: {every_weeks: 1}\n"
    (p,), errors = parse_profiles(text, None)
    assert not errors
    p = replace(
        p,
        outbound=replace(p.outbound, first_train=time(2, 5), window=(time(2), time(2, 15))),
        return_=replace(p.return_, first_train=time(2, 35), window=(time(2, 30), time(2, 45))),
    )

    (collision,) = find_overlaps([p], date(2026, 10, 1))
    assert "2026-10-25" in collision.message

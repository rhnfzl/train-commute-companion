"""Profile schema of window mode, minimum stay and per-day overrides (0.3.0)."""

from datetime import date, datetime, time
from pathlib import Path

import pytest

from train_commute_companion.model import DayOverride, Direction
from train_commute_companion.profile import parse_profiles
from train_commute_companion.schedule import dated_schedule

STATIONS = frozenset({"AAA", "UT", "RTD"})
BASE = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon, tue, wed, thu, fri, sat]
    home_station: "AAA"
    work_station: "RTD"
    outbound: OUT
    return: RET
    location: LOC
"""
FIXED_OUT = '{first_train: "08:12", buffer_min: 10}'
FIXED_RET = '{first_train: "17:52", buffer_min: 5}'


def parse(out: str = FIXED_OUT, ret: str = FIXED_RET, loc: str = "null"):
    text = BASE.replace("OUT", out).replace("RET", ret).replace("LOC", loc)
    return parse_profiles(text, STATIONS)


def errors(out: str = FIXED_OUT, ret: str = FIXED_RET) -> list[tuple[str, str]]:
    return [(e.path.removeprefix("profiles[0]."), e.message) for e in parse(out, ret)[1]]


def test_window_mode_profile_parses_without_first_train():
    (p,), errs = parse(out='{mode: window, window: ["07:00", "10:00"], buffer_min: 10}')
    assert errs == []
    j = p.journey(Direction.OUTBOUND)
    assert (j.mode, j.first_train, j.window, j.pick) == (
        "window",
        time(7),
        (time(7), time(10)),
        "earliest",
    )
    assert not j.first_train_set


def test_fixed_profiles_unchanged():
    total = 0
    for path in (Path("examples/profiles.yaml"), *Path("tests/data/profiles").glob("*.yaml")):
        profiles, errs = parse_profiles(path.read_text(), None)
        assert errs == [] or path.name.startswith("bad"), (path, errs)
        total += len(profiles)
        for p in profiles:
            for d in Direction:
                j = p.journey(d)
                assert (j.mode, j.pick, j.min_stay_min, j.by_weekday) == (
                    "fixed",
                    "earliest",
                    None,
                    {},
                )
    assert total >= 20  # the 0.2.6 fixtures really parse


W = 'mode: window, window: ["07:00", "10:00"], buffer_min: 10'


@pytest.mark.parametrize(
    ("out", "path", "message"),
    [
        ('{first_train: "08:12", buffer_min: 10, mode: train}', "outbound.mode",
         "must be fixed or window, got 'train'"),
        ("{mode: window, buffer_min: 10}", "outbound.window",
         "mode window needs a window: the earliest and latest departure"),
        ('{mode: window, window: ["07:00", "07:00"], buffer_min: 10}', "outbound.window",
         "window is empty: the latest time must differ from the earliest"),
        ("{buffer_min: 10}", "outbound.first_train", "not a valid time: None"),
        (f"{{{W}, pick: soon}}", "outbound.pick",
         "must be earliest, earliest_silent, on_leave or arrive_by, got 'soon'"),
        ('{first_train: "08:12", buffer_min: 10, pick: earliest}', "outbound.pick",
         "pick has no effect: no day uses mode window"),
        (f"{{{W}, min_stay_min: 0}}", "outbound.min_stay_min",
         "must be a whole number of minutes from 1 to 1440, got 0"),
        ('{first_train: "08:12", buffer_min: 10, min_stay_min: 60}', "outbound.min_stay_min",
         "min_stay_min has no effect: no day uses mode window"),
        (f"{{{W}, by_weekday: [mon]}}", "outbound.by_weekday",
         "must map mon through sun to overrides"),
        (f"{{{W}, by_weekday: {{funday: {{}}}}}}", "outbound.by_weekday.funday",
         "unknown field 'funday'"),
        (f"{{{W}, by_weekday: {{mon: {{first_train: '08:00'}}}}}}",
         "outbound.by_weekday.mon.first_train", "a day's first train is set in first_train_by_weekday"),
        (f"{{{W}, by_weekday: {{sat: {{mode: fixed}}}}}}", "outbound.by_weekday.sat.mode",
         "mode fixed needs a first train on sat"),
        ((f"{{{W}, first_train_by_weekday: {{sat: '10:12'}}, "
          "by_weekday: {sat: {mode: fixed, window: ['10:00', '12:00']}}}"),
         "outbound.by_weekday.sat.window",
         "window opens 12 min before first_train, needs at least 25 (buffer_min + 15)"),
        (f"{{{W}, by_weekday: {{fri: {{window: ['09:00', '09:00']}}}}}}",
         "outbound.by_weekday.fri.window",
         "window is empty: the latest time must differ from the earliest"),
        ('{first_train: "08:12", buffer_min: 10, by_weekday: {fri: {mode: window}}}',
         "outbound.by_weekday.fri.window",
         "mode window needs a window: the earliest and latest departure"),
        ('{first_train: "08:12", buffer_min: 10, by_weekday: {fri: {pick: on_leave}}}',
         "outbound.by_weekday.fri.pick", "pick has no effect on a fixed day"),
    ],
)  # fmt: skip
def test_window_mode_validation_errors(out, path, message):
    assert errors(out).count((path, message)) == 1


def test_window_mode_errors_all_come_in_one_pass():
    out = f"{{{W}, pick: soon, min_stay_min: -1, by_weekday: {{sat: {{mode: fixed}}}}}}"
    ret = "{mode: window, buffer_min: 5}"
    got = {p for p, _ in errors(out, ret)}
    assert {
        "outbound.pick",
        "outbound.min_stay_min",
        "outbound.by_weekday.sat.mode",
        "return.window",
    } <= got


def test_per_day_overrides_resolve_mode_window_pick_and_stay():
    out = (
        '{first_train: "08:12", buffer_min: 10, first_train_by_weekday: {tue: "08:42"}, '
        'by_weekday: {fri: {mode: window, window: ["09:00", "11:00"], pick: earliest_silent}, '
        'sat: {mode: window, window: ["10:00", "13:00"], min_stay_min: 30}}}'
    )
    (p,), errs = parse(out=out)
    assert errs == []
    mon, tue, fri, sat = (date(2026, 10, 26 + i) for i in (0, 1, 4, 5))
    assert p.journey_on(Direction.OUTBOUND, mon).mode == "fixed"
    assert p.journey_on(Direction.OUTBOUND, tue).first_train == time(8, 42)  # still works
    f = p.journey_on(Direction.OUTBOUND, fri)
    assert (f.mode, f.window, f.pick) == ("window", (time(9), time(11)), "earliest_silent")
    assert p.journey_on(Direction.OUTBOUND, sat).min_stay_min == 30
    assert p.journey(Direction.OUTBOUND).by_weekday["fri"] == DayOverride(
        "window", (time(9), time(11)), "earliest_silent", None
    )


def test_a_fixed_day_of_a_window_direction_watches_around_its_own_train():
    out = f"{{{W}, first_train_by_weekday: {{sat: '10:12'}}, by_weekday: {{sat: {{mode: fixed}}}}}}"
    (p,), errs = parse(out=out)
    assert errs == []
    sat = p.journey_on(Direction.OUTBOUND, date(2026, 10, 31))
    assert (sat.mode, sat.first_train, sat.window) == ("fixed", time(10, 12), (time(9, 12),
                                                                               time(12, 12)))  # fmt: skip


def test_pick_on_leave_without_location_falls_back_to_earliest_silent():
    out = f"{{{W}, pick: on_leave}}"
    (p,), _ = parse(out=out)
    assert p.journey_on(Direction.OUTBOUND, date(2026, 10, 27)).pick == "earliest_silent"
    (p,), _ = parse(out=out, loc="{source: ha, entity: person.me}")
    assert p.journey_on(Direction.OUTBOUND, date(2026, 10, 27)).pick == "on_leave"


AMS = datetime(2026, 10, 27, tzinfo=None)


def test_window_schedule_opens_active_lead_before_the_earliest_and_stay_moves_it():
    ret = '{mode: window, window: ["15:00", "20:00"], buffer_min: 5, min_stay_min: 480}'
    (p,), errs = parse(ret=ret)
    assert errs == []
    day = date(2026, 10, 27)
    s = dated_schedule(p, Direction.RETURN, day, None)
    assert (f"{s.first_departure:%H:%M}", f"{s.window_start:%H:%M}", f"{s.window_end:%H:%M}") == (
        "15:00",
        "14:40",
        "20:01",
    )
    arrived = s.first_departure.replace(hour=9, minute=5)
    assert f"{dated_schedule(p, Direction.RETURN, day, None, arrived).first_departure:%H:%M}" == (
        "17:05"
    )
    early = s.first_departure.replace(hour=6)
    assert f"{dated_schedule(p, Direction.RETURN, day, None, early).first_departure:%H:%M}" == (
        "15:00"
    )
    late = s.first_departure.replace(hour=13)
    capped = dated_schedule(p, Direction.RETURN, day, None, late)
    assert f"{capped.first_departure:%H:%M}" == "20:00" == f"{capped.last_departure:%H:%M}"


def test_first_train_in_a_day_points_to_first_train_by_weekday():
    out = '{first_train: "08:12", buffer_min: 10, by_weekday: {sat: {first_train: "10:12"}}}'
    _, errs = parse(out=out)
    (e,) = errs
    assert e.path.endswith("outbound.by_weekday.sat.first_train")
    assert "first_train_by_weekday" in e.message
    assert e.suggestion == 'set outbound.first_train_by_weekday.sat: "10:12"'


def test_a_day_stay_of_zero_is_off_and_inherits_nothing():
    out = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, min_stay_min: 480, by_weekday: {sun: {min_stay_min: 0}}}'
    (p,), errs = parse(out=out)
    assert errs == []
    from train_commute_companion.model import resolve_day

    assert resolve_day(p.outbound, "sun", True).min_stay_min == 0
    assert resolve_day(p.outbound, "mon", True).min_stay_min == 480
    fixed = '{first_train: "08:12", buffer_min: 10, by_weekday: {sun: {min_stay_min: 0}}}'
    assert parse(out=fixed)[1] == []  # 0 is off, so it has an effect nowhere and errs nowhere

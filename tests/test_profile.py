from datetime import date, datetime, time
from pathlib import Path

import pytest

from train_commute_companion.model import Config, Direction
from train_commute_companion.profile import (
    ProfileError,
    check_lines,
    find_overlaps,
    load_config,
    parse_profiles,
    slug,
)

DATA = Path(__file__).parent / "data" / "profiles"
TODAY = date(2026, 10, 26)
STATIONS = frozenset({"AAA", "UT", "RTD"})

BAD_YAML = """
profiles:
  - name: "office_a"
    days: [tue]
    home_station: "ZZZ"
    work_station: "UT"
    outbound: {first_train: "08:12", transfers: ["UT"], window: ["08:12", "10:12"], buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""


def load(name: str):
    profiles, errors = parse_profiles((DATA / name).read_text(), None)
    assert errors == []
    return profiles


def one(*pairs: tuple[str, str]) -> str:
    text = (DATA / "tue_wed_and_thu.yaml").read_text().split("  - <<")[0]
    for old, new in pairs:
        assert old in text, old
        text = text.replace(old, new)
    return text


GOOD_CFG = Config(1, tuple(load("tue_wed_and_thu.yaml")))


def test_starter_file_is_valid():
    profiles, errors = parse_profiles(Path("examples/profiles.yaml").read_text(), None)
    assert errors == [] and [p.name for p in profiles] == ["office_a", "office_b"]


def test_starter_file_valid_with_station_list():
    _, errors = parse_profiles(Path("examples/profiles.yaml").read_text(), STATIONS)
    assert errors == []


def test_two_profiles_different_days_ok():
    assert find_overlaps(load("tue_wed_and_thu.yaml"), TODAY) == []


def test_two_profiles_same_day_refused():
    errs = find_overlaps(load("both_on_wed.yaml"), TODAY)
    assert len(errs) == 1 and "office_a" in errs[0].message and "office_b" in errs[0].message
    assert "wed" in errs[0].message.lower()


def test_extra_date_overlaps_weekday():
    a, b = load("tue_wed_and_thu.yaml")
    from dataclasses import replace

    b = replace(b, days=frozenset(), extra_dates=frozenset({date(2026, 10, 27)}))
    assert len(find_overlaps([a, b], TODAY)) == 1


def test_shared_weekday_overlaps_even_when_horizon_only_has_a_holiday():
    from dataclasses import replace

    a, b = load("both_on_wed.yaml")
    a, b = replace(a, days=frozenset({5})), replace(b, days=frozenset({5}))
    holiday = date(
        2026, 12, 25
    )  # a Friday and a holiday: both skip it, yet every other Friday collides
    assert a.skip_public_holidays and b.skip_public_holidays
    assert len(find_overlaps([a, b], holiday)) == 1


def test_load_config_refuses_shared_weekday_that_is_a_holiday_this_week():
    text = (DATA / "both_on_wed.yaml").read_text()
    cfg, errs = load_config(
        text, None, GOOD_CFG, date(2026, 12, 21)
    )  # both profiles share Wednesday, so the file is refused
    assert cfg is None and errs
    fri = text.replace("[tue, wed]", "[fri]").replace("[wed]", "[fri]")
    cfg, errs = load_config(fri, None, GOOD_CFG, date(2026, 12, 21))
    assert cfg is None and len(errs) == 1 and "fri" in errs[0].message


def test_every_error_is_reported():
    _, errs = parse_profiles(BAD_YAML, STATIONS)
    paths = {e.path for e in errs}
    assert {
        "profiles[0].id",
        "profiles[0].transfer_min.UT",
        "profiles[0].outbound.window",
        "profiles[0].home_station",
    } <= paths
    assert any(e.suggestion and "transfer_min" in e.suggestion for e in errs)


def test_day_offset_on_outbound_refused():
    text = one(("outbound: {first_train", "outbound: {day_offset: 1, first_train"))
    _, errs = parse_profiles(text, None)
    assert [e.path for e in errs] == ["profiles[0].outbound.day_offset"]


def test_day_offset_on_return_allowed():
    text = one(("return: {first_train", "return: {day_offset: 1, first_train"))
    profiles, errs = parse_profiles(text, None)
    assert errs == [] and profiles[0].return_.day_offset == 1


def test_second_traveller_value_refused():
    text = one(("days: [tue, wed]", 'traveller: "partner"\n    days: [tue, wed]'))
    _, errs = parse_profiles(text, None)
    assert [e.path for e in errs] == ["profiles[0].traveller"]


def test_duplicate_id_and_name_refused():
    text = (DATA / "tue_wed_and_thu.yaml").read_text()
    profiles, errs = parse_profiles(text.replace('"p2"', '"p1"'), None)
    assert any("duplicate id" in e.message for e in errs) and profiles == []
    profiles, errs = parse_profiles(text.replace("office_b", "Office-A"), None)
    assert any("duplicate name" in e.message for e in errs) and profiles == []


def test_window_must_open_buffer_plus_15_before_first_train():
    base = one(("outbound: {first_train", 'outbound: {window: ["07:12", "10:12"], first_train'))
    assert parse_profiles(base.replace("buffer_min: 10", "buffer_min: 45", 1), None)[1] == []
    _, errs = parse_profiles(base.replace("buffer_min: 10", "buffer_min: 46", 1), None)
    assert [e.path for e in errs] == ["profiles[0].outbound.window"]
    assert "07:11" in (errs[0].suggestion or "")


def test_window_may_cross_midnight():
    text = one(
        ("return: {first_train", 'return: {window: ["23:00", "03:00"], first_train')
    ).replace("17:52", "23:40")
    profiles, errs = parse_profiles(text, None)
    assert errs == [] and profiles[0].return_.window == (time(23, 0), time(3, 0))


def test_window_must_contain_first_train():
    text = one(("outbound: {first_train", 'outbound: {window: ["09:24", "10:12"], first_train'))
    _, errs = parse_profiles(text, None)
    assert "does not contain" in errs[0].message


def test_invalid_edit_keeps_previous_config():
    cfg, errs = load_config(BAD_YAML, None, previous=GOOD_CFG, today=TODAY)
    assert cfg is None and errs


def test_valid_edit_bumps_revision_and_same_text_does_not():
    text = (DATA / "tue_wed_and_thu.yaml").read_text()
    same, _ = load_config(text, None, GOOD_CFG, TODAY)
    assert same is GOOD_CFG
    new, errs = load_config(text.replace("[thu]", "[fri]"), None, GOOD_CFG, TODAY)
    assert errs == [] and new is not None and new.revision == 2


def test_overlap_refuses_whole_new_version():
    text = (DATA / "both_on_wed.yaml").read_text()
    cfg, errs = load_config(text, None, GOOD_CFG, TODAY)
    assert cfg is None and len(errs) == 1


def test_first_start_runs_valid_profiles_and_refuses_overlap_pair():
    text = (DATA / "both_on_wed.yaml").read_text() + (
        '  - id: "p3"\n    name: "office_c"\n    days: [fri]\n    home_station: "AAA"\n'
        '    work_station: "UT"\n    outbound: {first_train: "08:12", buffer_min: 10}\n'
        '    return: {first_train: "17:52", buffer_min: 5}\n'
    )
    cfg, errs = load_config(text, None, None, TODAY)
    assert cfg is not None and [p.id for p in cfg.profiles] == ["p3"] and len(errs) == 1


def test_first_start_with_nothing_valid_gives_none():
    cfg, errs = load_config(BAD_YAML, None, None, TODAY)
    assert cfg is None and errs


def test_bom_crlf_file_loads():
    profiles, errs = parse_profiles((DATA / "bom_crlf.yaml").read_bytes().decode("utf-8"), None)
    assert errs == [] and [p.id for p in profiles] == ["p1"]


def test_tab_indent_gives_line_error():
    _, errs = parse_profiles("profiles:\n\t- id: x\n", None)
    assert errs and "line 2" in errs[0].message


def test_garbage_input_never_crashes():
    for text in ("", "- 1", "profiles: 3", "profiles: [1]", "\x00"):
        assert isinstance(parse_profiles(text, None)[1][0], ProfileError)


def test_unquoted_time_gets_a_hint():
    _, errs = parse_profiles(one(('"17:52"', "17:52")), None)
    assert errs and "quoted" in (errs[0].suggestion or "")


def test_default_window_is_minus_60_plus_120():
    p = load("tue_wed_and_thu.yaml")[0]
    assert p.outbound.window == (time(7, 12), time(10, 12))
    assert p.return_.window == (time(16, 52), time(19, 52))


def test_profile_helpers():
    p = load("tue_wed_and_thu.yaml")[0]
    assert p.origin(Direction.OUTBOUND) == "AAA" and p.destination(Direction.OUTBOUND) == "UT"
    assert p.origin(Direction.RETURN) == "UT" and p.journey(Direction.RETURN) is p.return_
    assert p.days == frozenset({2, 3}) and p.return_.earlier_train_advice is True


@pytest.mark.parametrize("name,expected", [("Office A!", "office_a"), ("  x-y ", "x_y")])
def test_slug(name: str, expected: str):
    assert slug(name) == expected


def test_duplicate_name_also_refuses_valid_twin_of_an_invalid_profile():
    text = (DATA / "tue_wed_and_thu.yaml").read_text().replace("office_b", "office_a")
    text = text.replace('home_station: "AAA"', 'home_station: "ZZZ"', 1)  # first entry only
    profiles, errs = parse_profiles(text, STATIONS)
    assert profiles == [] and any("duplicate name" in e.message for e in errs)


def test_shared_extra_date_beyond_a_year_is_refused():
    text = (
        (DATA / "tue_wed_and_thu.yaml")
        .read_text()
        .replace("days: [thu]", "days: [thu]\n    extra_dates: [2030-03-05]")
    )
    text = text.replace("days: [tue, wed]", "days: [tue, wed]\n    extra_dates: [2030-03-05]")
    cfg, errs = load_config(text, None, GOOD_CFG, TODAY)
    assert cfg is None and any("2030-03-05" in e.message for e in errs)


def test_night_windows_overlap_across_midnight_refused():
    errs = find_overlaps(load("night_overlap.yaml"), TODAY)
    assert errs and "2026-10-28" in errs[0].message


NIGHT = (DATA / "night_overlap.yaml").read_text()


def refused(text: str, today: date = TODAY) -> list[ProfileError]:
    """Errors from both a first start and a reload; both must refuse."""
    first, errs = load_config(text, None, None, today)
    reload, errs2 = load_config(text, None, GOOD_CFG, today)
    assert reload is None and errs == errs2
    assert first is None or all(p.id == "p3" for p in first.profiles)
    return errs


@pytest.mark.parametrize(
    "value", ["2026-02-30", "2026-10-27T00:00:00", "'2026-02-30'", "20261027", "!!timestamp x"]
)
def test_bad_extra_date_is_a_profile_error(value: str):
    text = one(("days: [tue, wed]", f"days: [tue, wed]\n    extra_dates: [{value}]"))
    errs = refused(text)
    assert [e.path for e in errs] == ["profiles[0].extra_dates[0]"]


def test_unquoted_extra_date_still_loads():
    text = one(("days: [tue, wed]", "days: [tue, wed]\n    extra_dates: [2026-11-05]"))
    profiles, errs = parse_profiles(text, None)
    assert errs == [] and profiles[0].extra_dates == frozenset({date(2026, 11, 5)})


def test_explicit_bad_tag_is_a_profile_error():
    _, errs = parse_profiles("profiles: [!!int abc]", None)
    assert errs and errs[0].path == "file"


def test_far_extra_date_on_another_profiles_weekday_is_refused():
    text = (DATA / "tue_wed_and_thu.yaml").read_text()
    text = text.replace("days: [thu]", "days: []\n    extra_dates: [2030-03-05]")  # a Tuesday
    errs = refused(text)
    assert len(errs) == 1 and "2030-03-05" in errs[0].message


def test_adjacent_extra_dates_with_overlapping_night_windows_are_refused():
    text = NIGHT.replace("days: [tue]", "days: []\n    extra_dates: [2030-03-05]")
    text = text.replace("days: [wed]", "days: []\n    extra_dates: [2030-03-06]")
    errs = refused(text)
    assert len(errs) == 1 and "2030-03-06" in errs[0].message


def test_recurring_night_overlap_hidden_by_holidays_this_week_is_refused():
    # Fri 2026-12-25 and Fri 2027-01-01 are holidays, so the next week shows no collision
    text = NIGHT.replace("[tue]", "[thu]").replace("[wed]", "[fri]")
    errs = refused(text, date(2026, 12, 24))
    assert len(errs) == 1 and "overlapping" in errs[0].message


def test_overlap_only_on_the_autumn_dst_night_is_refused():
    text = NIGHT.replace("[tue]", "[sat]").replace('"01:30"', '"02:30"')
    text = text.replace("[wed]", "[sun]").replace(
        'window: ["00:00", "05:00"]', 'window: ["02:45", "05:00"]'
    )
    # Sat window ends at the second 02:30, Sun window opens at the first 02:45: an hour of overlap
    assert (
        "2026-10-25" in find_overlaps(parse_profiles(text, None)[0], date(2026, 10, 20))[0].message
    )
    errs = refused(text, date(2026, 10, 26))  # this year's night is past, next year's still counts
    assert len(errs) == 1 and "2027-10-31" in errs[0].message
    ordinary = text.replace('"02:30"', '"01:30"')
    assert find_overlaps(parse_profiles(ordinary, None)[0], date(2026, 10, 20)) == []


def test_outbound_and_return_of_one_profile_must_not_overlap():
    errs = refused(one(('"17:52"', '"08:12"')))
    assert len(errs) == 1 and "office_a" in errs[0].message and "overlapping" in errs[0].message


def test_next_day_return_overlapping_next_outbound_is_refused():
    text = one(
        ("return: {first_train", "return: {day_offset: 1, first_train"), ('"17:52"', '"08:12"')
    )
    errs = refused(text)
    assert len(errs) == 1 and "2026-10-28" in errs[0].message  # Tue's return meets Wed's outbound


def test_night_shift_return_next_morning_is_fine():
    text = one(
        ("return: {first_train", "return: {day_offset: 1, first_train"), ('"17:52"', '"04:00"')
    )
    profiles, errs = parse_profiles(text, None)
    assert errs == [] and find_overlaps(profiles, TODAY) == []


def test_touching_windows_are_allowed_and_only_the_later_one_is_active():
    from train_commute_companion.calendar import TZ
    from train_commute_companion.schedule import schedules_active

    text = NIGHT.replace('window: ["00:00", "05:00"]', 'window: ["01:30", "05:00"]')
    profiles, errs = parse_profiles(text, None)
    assert errs == [] and find_overlaps(profiles, TODAY) == []
    at = datetime(2026, 10, 28, 1, 30, tzinfo=TZ)  # Tue window ends, Wed window opens
    assert [s.key.profile_id for s in schedules_active(Config(1, tuple(profiles)), {}, at)] == [
        "p2"
    ]


P3 = """
  - id: "p3"
    name: "office_c"
    days: [fri]
    home_station: "AAA"
    work_station: "UT"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""


@pytest.mark.parametrize(
    ("leg", "window", "first"),
    [
        ("outbound", '["07:12", "08:12"]', "08:12"),
        ("return", '["16:00", "17:52"]', "17:52"),
        ("return", '["23:00", "00:10"]', "00:10"),  # crosses midnight
    ],
)
def test_window_closing_at_first_train_is_refused(leg: str, window: str, first: str):
    old = "08:12" if leg == "outbound" else "17:52"
    text = one(
        (f'{leg}: {{first_train: "{old}"', f'{leg}: {{window: {window}, first_train: "{first}"')
    )
    _, errs = parse_profiles(text, None)
    assert [e.path for e in errs] == [f"profiles[0].{leg}.window"]


def test_window_one_minute_past_first_train_contains_the_departure():
    from train_commute_companion.schedule import dated_schedule, in_window

    text = one(("outbound: {first_train", 'outbound: {window: ["07:12", "08:13"], first_train'))
    profiles, errs = parse_profiles(text, None)
    assert errs == []
    s = dated_schedule(profiles[0], Direction.OUTBOUND, date(2026, 10, 27), None)
    assert in_window(s, s.first_departure)


def test_shared_past_extra_date_with_next_day_returns_still_open_is_refused():
    text = (
        (DATA / "tue_wed_and_thu.yaml")
        .read_text()
        .replace("days: [tue, wed]", "days: []\n    extra_dates: [2026-10-27]")
    )
    text = text.replace("return: {first_train", "return: {day_offset: 1, first_train")
    text = text.replace("days: [thu]", "days: []") + P3
    errs = refused(text, date(2026, 10, 28))  # both returns run on 2026-10-28
    assert len(errs) == 1 and "office_a" in errs[0].message and "office_b" in errs[0].message


@pytest.mark.parametrize("extreme", ["0001-01-01", "0001-01-03", "9999-12-29", "9999-12-31"])
def test_extra_date_beyond_the_schedulable_range_is_a_profile_error(extreme: str):
    text = (
        (DATA / "tue_wed_and_thu.yaml")
        .read_text()
        .replace("days: [thu]", f"days: []\n    extra_dates: [{extreme}]")
    )
    first, errs = load_config(text + P3, None, None, TODAY)
    reload, errs2 = load_config(text + P3, None, GOOD_CFG, TODAY)
    assert reload is None and errs == errs2
    assert [e.path for e in errs] == ["profiles[1].extra_dates[0]"]
    assert first is not None and [p.id for p in first.profiles] == ["p1", "p3"]


LOCATED = (DATA / "located.yaml").read_text()


def with_radius(text: str, m: object) -> str:
    assert "    location:\n" in text
    return text.replace("    location:\n", f"    location:\n      station_radius_m: {m!r}\n")


def test_check_lines_names_errors_suggestions_and_dst():
    _, lines = check_lines((DATA / "bad_starter.yaml").read_text(), STATIONS, TODAY)
    assert any("transfer_min" in x and x.endswith(")") for x in lines)  # suggestion shown
    assert any(x.startswith("id") or ".id:" in x for x in lines)  # the missing id is named
    assert any(x.startswith("warning:") and "clock-change nights" in x for x in lines)


def test_station_radius_default_and_custom():
    assert parse_profiles(LOCATED, STATIONS)[0][0].location.station_radius_m == 150
    assert (
        parse_profiles(with_radius(LOCATED, 300), STATIONS)[0][0].location.station_radius_m == 300
    )


@pytest.mark.parametrize("bad", [0, -5, "150", 1.5, True])
def test_station_radius_must_be_positive(bad):
    _, errors = parse_profiles(with_radius(LOCATED, bad), STATIONS)
    assert [e.path for e in errors] == ["profiles[0].location.station_radius_m"]
    assert errors[0].message == "must be a whole number of metres above 0"

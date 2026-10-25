from datetime import date, time
from pathlib import Path

import pytest

from train_commute_companion.model import Config, Direction
from train_commute_companion.profile import (
    ProfileError,
    find_overlaps,
    load_config,
    parse_profiles,
    slug,
)

DATA = Path(__file__).parent / "data" / "profiles"
HORIZON = (date(2026, 10, 26), date(2026, 11, 8))
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
    assert find_overlaps(load("tue_wed_and_thu.yaml"), HORIZON) == []


def test_two_profiles_same_day_refused():
    errs = find_overlaps(load("both_on_wed.yaml"), HORIZON)
    assert len(errs) == 1 and "office_a" in errs[0].message and "office_b" in errs[0].message
    assert "wed" in errs[0].message.lower()


def test_extra_date_overlaps_weekday():
    a, b = load("tue_wed_and_thu.yaml")
    from dataclasses import replace

    b = replace(b, days=frozenset(), extra_dates=frozenset({date(2026, 10, 27)}))
    assert len(find_overlaps([a, b], HORIZON)) == 1


def test_shared_weekday_overlaps_even_when_horizon_only_has_a_holiday():
    from dataclasses import replace

    a, b = load("both_on_wed.yaml")
    a, b = replace(a, days=frozenset({5})), replace(b, days=frozenset({5}))
    holiday = date(
        2026, 12, 25
    )  # a Friday and a holiday: both skip it, yet every other Friday collides
    assert a.skip_public_holidays and b.skip_public_holidays
    assert len(find_overlaps([a, b], (holiday, holiday))) == 1


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

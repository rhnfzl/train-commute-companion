import copy
from datetime import date
from pathlib import Path

import pytest
import yaml
from ha_rig import PROFILE

from custom_components.train_commute_companion import profile_editor
from custom_components.train_commute_companion.profile_editor import (
    EditorError,
    edit_profile,
    read_document,
    save_document,
)
from train_commute_companion.model import Stations
from train_commute_companion.profile import parse_profiles

KNOWN = frozenset({"AAA", "UT", "RTD", "BBB"})
TODAY = date(2026, 11, 6)
VALUES = {
    "name": "commute",
    "days": ["mon", "tue"],
    "home_station": "AAA",
    "work_station": "RTD",
    "outbound_time": "08:12",
    "return_time": "17:52",
    "outbound_transfers": ["UT"],
    "return_transfers": [],
    "outbound_buffer": 10,
    "return_buffer": 5,
    "skip_public_holidays": True,
}


def document(tmp_path, text=PROFILE):
    path = tmp_path / "profiles.yaml"
    if text is not None:
        path.write_text(text)
    return read_document(path, (tmp_path,), KNOWN, TODAY, create=text is None)


def test_new_profile_is_valid_and_written_only_when_saved(tmp_path):
    doc = document(tmp_path, None)
    text = edit_profile(doc, None, VALUES, KNOWN, TODAY)
    assert not doc.path.exists()
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and len(profiles) == 1
    assert profiles[0].outbound.transfers == ("UT",)
    save_document(doc, text, (tmp_path,))
    assert doc.path.read_text() == text


def test_ntfy_switch_roundtrips_and_preserves_other_alert_settings(tmp_path):
    doc = document(tmp_path)
    for value in (False, True):
        text = edit_profile(
            doc, "p1", {**VALUES, "ntfy_enabled": value, "leave_by": False}, KNOWN, TODAY
        )
        profiles, errors = parse_profiles(text, KNOWN)
        assert not errors
        assert profiles[0].alerts.ntfy_enabled is value
        assert profiles[0].alerts.leave_by is False
        doc = document(tmp_path, text)


def test_check_in_times_roundtrip_and_can_return_to_default(tmp_path):
    doc = document(tmp_path)
    values = {**VALUES, "outbound_active_lead": 40.0, "return_active_lead": 25, "leave_by": False}
    text = edit_profile(doc, "p1", values, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors
    assert profiles[0].outbound.active_lead_min == 40
    assert profiles[0].return_.active_lead_min == 25
    assert profiles[0].alerts.leave_by is False
    saved = document(tmp_path, text)
    text = edit_profile(
        saved,
        "p1",
        {**values, "outbound_active_lead": None, "return_active_lead": None, "leave_by": True},
        KNOWN,
        TODAY,
    )
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors
    assert profiles[0].outbound.active_lead_min is None
    assert profiles[0].return_.active_lead_min is None
    assert profiles[0].alerts.leave_by is True
    raw = yaml.safe_load(text)["profiles"][0]
    assert "active_lead_min" not in raw["outbound"]
    assert "active_lead_min" not in raw["return"]


@pytest.mark.parametrize("value", [-1, 9, 10.5, True, "40"])
def test_invalid_check_in_time_does_not_write(tmp_path, value):
    doc = document(tmp_path)
    with pytest.raises(EditorError, match="invalid_profile"):
        edit_profile(doc, "p1", {**VALUES, "outbound_active_lead": value}, KNOWN, TODAY)
    assert doc.path.read_text() == PROFILE


def test_check_in_default_remains_omitted_and_leave_by_defaults_on(tmp_path):
    text = edit_profile(document(tmp_path, None), None, VALUES, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and profiles[0].outbound.active_lead_min is None
    assert profiles[0].alerts.leave_by is True
    assert profiles[0].alerts.weather_allowance is True
    assert "active_lead_min" not in yaml.safe_load(text)["profiles"][0]["outbound"]


@pytest.mark.parametrize("enabled", [True, False])
def test_weather_allowance_roundtrip_preserves_independent_switches(tmp_path, enabled):
    text = edit_profile(
        document(tmp_path),
        "p1",
        {**VALUES, "weather_allowance": enabled, "weather": False, "leave_by": True},
        KNOWN,
        TODAY,
    )
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors
    alerts = profiles[0].alerts
    assert (
        alerts.weather_allowance is enabled and alerts.weather is False and alerts.leave_by is True
    )
    text = edit_profile(document(tmp_path, text), "p1", VALUES, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and profiles[0].alerts.weather_allowance is enabled
    with pytest.raises(EditorError, match="invalid_profile"):
        edit_profile(
            document(tmp_path), "p1", {**VALUES, "weather_allowance": "false"}, KNOWN, TODAY
        )


@pytest.mark.parametrize("enabled", [True, False])
def test_weather_switch_preserves_other_alert_settings(tmp_path, enabled):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["alerts"] = {"quiet_hours": ["20:00", "06:00"]}
    doc = document(tmp_path, yaml.safe_dump(raw))
    text = edit_profile(doc, "p1", {**VALUES, "weather": enabled}, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and profiles[0].alerts.weather is enabled
    assert yaml.safe_load(text)["profiles"][0]["alerts"]["quiet_hours"] == ["20:00", "06:00"]


@pytest.mark.parametrize("weather", [False, True])
@pytest.mark.parametrize("leave_by", [False, True])
def test_weather_leave_by_and_delay_edits_stay_independent(tmp_path, weather, leave_by):
    values = {
        **VALUES,
        "weather": weather,
        "leave_by": leave_by,
        "delay_min": 10,
        "delay_step_min": 5,
        "outbound_active_lead": 40,
        "return_active_lead": 25,
    }
    text = edit_profile(document(tmp_path), "p1", values, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors
    profile = profiles[0]
    assert profile.alerts.weather is weather
    assert profile.alerts.leave_by is leave_by
    assert (profile.alerts.delay_min, profile.alerts.delay_step_min) == (10, 5)
    assert profile.outbound.active_lead_min == 40
    assert profile.return_.active_lead_min == 25


def test_edit_preserves_id_and_all_advanced_fields(tmp_path):
    raw = yaml.safe_load(PROFILE)
    p = raw["profiles"][0]
    p.update(extra_dates=["2027-03-02"], pauses=[{"start": "2027-04-01", "end": "2027-04-07"}])
    p["alerts"] = {"quiet_hours": ["20:00", "06:00"]}
    p["return"].update(day_offset=1, window=["16:52", "19:52"])
    p["outbound"]["active_lead_min"] = 40
    raw["profiles"][1]["days"] = ["sun"]
    original = copy.deepcopy(raw)
    doc = document(tmp_path, yaml.safe_dump(raw))
    changed = yaml.safe_load(edit_profile(doc, "p1", {**VALUES, "name": "renamed"}, KNOWN, TODAY))
    p = changed["profiles"][0]
    assert p["id"] == "p1"
    for field in ("extra_dates", "pauses", "alerts"):
        assert p[field] == original["profiles"][0][field]
    assert p["return"]["day_offset"] == 1
    assert p["return"]["window"] == ["16:52", "19:52"]
    assert p["outbound"]["active_lead_min"] == 40
    assert changed["profiles"][1] == original["profiles"][1]


def test_transfer_minimum_and_holiday_ranges_can_be_edited(tmp_path):
    doc = document(tmp_path)
    text = edit_profile(
        doc,
        "p1",
        {
            **VALUES,
            "transfer_min": {"UT": 9},
            "pause_action": "add",
            "pause_start": "2027-04-01",
            "pause_end": "2027-04-07",
        },
        KNOWN,
        TODAY,
    )
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors
    assert profiles[0].transfer_min["UT"] == 9
    assert profiles[0].paused_on(date(2027, 4, 7))
    assert not profiles[0].paused_on(date(2027, 4, 8))


def test_accessible_preference_can_be_changed_without_affecting_other_profiles(tmp_path):
    doc = document(tmp_path)
    original = copy.deepcopy(doc.data)
    text = edit_profile(doc, "p1", {**VALUES, "require_accessible": True}, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and profiles[0].accessibility.require_accessible
    assert yaml.safe_load(text)["profiles"][1] == original["profiles"][1]
    doc = document(tmp_path, text)
    text = edit_profile(doc, "p1", {**VALUES, "require_accessible": False}, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and not profiles[0].accessibility.require_accessible


def test_notification_categories_preserve_aliased_alert_settings(tmp_path):
    data = yaml.safe_load(PROFILE)
    shared = {"quiet_hours": ["20:00", "06:00"], "disabled_categories": ["routine"]}
    data["profiles"][0]["alerts"] = shared
    data["profiles"][1]["alerts"] = shared
    doc = document(tmp_path, yaml.safe_dump(data))
    text = edit_profile(doc, "p1", {**VALUES, "disabled_categories": ["crowding"]}, KNOWN, TODAY)
    saved = yaml.safe_load(text)
    assert saved["profiles"][0]["alerts"] == {**shared, "disabled_categories": ["crowding"]}
    assert saved["profiles"][1]["alerts"] == shared


def test_timetable_horizon_preserves_aliased_alert_settings(tmp_path):
    raw = yaml.safe_load(PROFILE)
    alerts = {
        "weather": False,
        "leave_by": False,
        "delay_min": 8,
        "quiet_hours": ["20:00", "06:00"],
    }
    raw["profiles"][0]["alerts"] = raw["profiles"][1]["alerts"] = alerts
    doc = document(tmp_path, yaml.safe_dump(raw))
    changes = {
        "custom_timetable_horizon": False,
        "timetable_horizon_unit": "commute_dates",
        "timetable_horizon_count": 14,
    }
    saved = yaml.safe_load(edit_profile(doc, "p1", {**VALUES, **changes}, KNOWN, TODAY))
    assert saved["profiles"][0]["id"] == "p1"
    assert saved["profiles"][0]["alerts"] == {**alerts, **changes}
    assert saved["profiles"][1] == raw["profiles"][1]


def test_quiet_repeat_misses_is_written_only_once_chosen(tmp_path):
    doc = document(tmp_path)
    off = yaml.safe_load(
        edit_profile(doc, "p1", {**VALUES, "quiet_repeat_misses": False}, KNOWN, TODAY)
    )
    assert "quiet_repeat_misses" not in off["profiles"][0].get("alerts", {})
    text = edit_profile(doc, "p1", {**VALUES, "quiet_repeat_misses": True}, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and profiles[0].alerts.quiet_repeat_misses
    with pytest.raises(EditorError):
        edit_profile(doc, "p1", {**VALUES, "quiet_repeat_misses": "yes"}, KNOWN, TODAY)


def test_delay_controls_roundtrip_without_changing_another_profile(tmp_path):
    doc = document(tmp_path)
    original = copy.deepcopy(doc.data["profiles"][1])
    text = edit_profile(
        doc, "p1", {**VALUES, "delay_min": 10.0, "delay_step_min": 5.0}, KNOWN, TODAY
    )
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors
    assert (profiles[0].alerts.delay_min, profiles[0].alerts.delay_step_min) == (10, 5)
    saved = yaml.safe_load(text)
    assert type(saved["profiles"][0]["alerts"]["delay_min"]) is int
    assert saved["profiles"][1] == original


def test_unchanged_delay_defaults_keep_an_older_profile_shape(tmp_path):
    doc = document(tmp_path)
    before = yaml.safe_load(edit_profile(doc, "p1", VALUES, KNOWN, TODAY))
    text = edit_profile(
        doc, "p1", {**VALUES, "delay_min": 3.0, "delay_step_min": 3.0}, KNOWN, TODAY
    )
    assert yaml.safe_load(text) == before


@pytest.mark.parametrize("field", ["delay_min", "delay_step_min"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3", None, 1441])
def test_invalid_delay_control_does_not_write(tmp_path, field, value):
    doc = document(tmp_path)
    with pytest.raises(EditorError) as error:
        edit_profile(doc, "p1", {**VALUES, field: value}, KNOWN, TODAY)
    assert f"alerts.{field}" in error.value.report
    assert "1440" in error.value.report
    assert doc.path.read_text() == PROFILE


def test_channel_editor_preserves_aliased_settings_and_can_restore_defaults(tmp_path):
    data = yaml.safe_load(PROFILE)
    shared = {
        "quiet_hours": ["20:00", "06:00"],
        "channels": {"delays": ["primary"], "crowding": ["webhook"]},
    }
    data["profiles"][0]["alerts"] = shared
    data["profiles"][1]["alerts"] = shared
    doc = document(tmp_path, yaml.safe_dump(data))
    text = edit_profile(
        doc,
        "p1",
        {**VALUES, "channel_journey": "primary,webhook", "channel_delays": "default"},
        KNOWN,
        TODAY,
        available_channels=frozenset({"primary", "webhook"}),
    )
    saved = yaml.safe_load(text)
    assert saved["profiles"][0]["alerts"] == {
        "quiet_hours": ["20:00", "06:00"],
        "channels": {"journey": ["primary", "webhook"], "crowding": ["webhook"]},
    }
    assert saved["profiles"][1]["alerts"] == shared


def test_unavailable_destinations_do_not_overwrite_the_profile_file(tmp_path):
    doc = document(tmp_path)
    with pytest.raises(EditorError, match="invalid_profile") as error:
        edit_profile(
            doc,
            "p1",
            {**VALUES, "channel_journey": "email"},
            KNOWN,
            TODAY,
            available_channels=frozenset({"primary"}),
        )
    assert "channels.journey" in error.value.report
    assert doc.path.read_text() == PROFILE


def test_weekday_departures_replace_and_clear_without_changing_other_profiles(tmp_path):
    doc = document(tmp_path)
    original = copy.deepcopy(doc.data["profiles"][1])
    text = edit_profile(
        doc,
        "p1",
        {
            **VALUES,
            "outbound_weekday_times": {"tue": "09:12"},
            "return_weekday_times": {"tue": "18:40"},
        },
        KNOWN,
        TODAY,
    )
    saved = yaml.safe_load(text)
    assert saved["profiles"][0]["outbound"]["first_train_by_weekday"] == {"tue": "09:12"}
    assert saved["profiles"][0]["return"]["first_train_by_weekday"] == {"tue": "18:40"}
    assert saved["profiles"][1] == original
    doc = document(tmp_path, text)
    text = edit_profile(doc, "p1", {**VALUES, "outbound_weekday_times": {}}, KNOWN, TODAY)
    saved = yaml.safe_load(text)
    assert "first_train_by_weekday" not in saved["profiles"][0]["outbound"]
    assert saved["profiles"][0]["return"]["first_train_by_weekday"] == {"tue": "18:40"}


def test_invalid_weekday_time_keeps_the_file_unchanged(tmp_path):
    doc = document(tmp_path)
    with pytest.raises(EditorError, match="invalid_profile"):
        edit_profile(
            doc, "p1", {**VALUES, "outbound_weekday_times": {"tue": "25:00"}}, KNOWN, TODAY
        )
    assert doc.path.read_text() == PROFILE


@pytest.mark.parametrize(
    "details",
    [
        {"transfer_min": {"UT": -1}},
        {"pause_action": "add", "pause_start": "2027-04-07", "pause_end": "2027-04-01"},
        {"pause_action": "add", "pause_start": "2027-04-01"},
    ],
)
def test_invalid_minimum_or_holiday_is_rejected(tmp_path, details):
    doc = document(tmp_path)
    with pytest.raises(EditorError, match="invalid_profile"):
        edit_profile(doc, "p1", {**VALUES, **details}, KNOWN, TODAY)
    assert doc.path.read_text() == PROFILE


def test_removing_holiday_ranges_preserves_other_settings(tmp_path):
    data = yaml.safe_load(PROFILE)
    data["profiles"][0]["pauses"] = [{"start": "2027-04-01", "end": "2027-04-07"}]
    doc = document(tmp_path, yaml.safe_dump(data))
    text = edit_profile(doc, "p1", {**VALUES, "pause_action": "clear"}, KNOWN, TODAY)
    saved = yaml.safe_load(text)
    assert saved["profiles"][0]["pauses"] == []
    assert saved["profiles"][1] == data["profiles"][1]


@pytest.mark.parametrize(
    "patch",
    [
        {"name": ""},
        {"home_station": "FORGED"},
        {"days": []},
        {"outbound_time": "25:00"},
        {"outbound_buffer": -1},
        {"outbound_transfers": ["FORGED"]},
    ],
)
def test_invalid_input_never_changes_the_file(tmp_path, patch):
    doc = document(tmp_path)
    with pytest.raises(EditorError, match="invalid_profile"):
        edit_profile(doc, "p1", {**VALUES, **patch}, KNOWN, TODAY)
    assert doc.path.read_text() == PROFILE


def test_duplicate_name_and_overlapping_profile_are_rejected(tmp_path):
    doc = document(tmp_path)
    with pytest.raises(EditorError, match="invalid_profile"):
        edit_profile(doc, None, {**VALUES, "name": "office_b"}, KNOWN, TODAY)
    with pytest.raises(EditorError, match="invalid_profile"):
        edit_profile(doc, None, VALUES, KNOWN, TODAY)


def test_invalid_source_is_not_rewritten(tmp_path):
    with pytest.raises(EditorError, match="invalid_profile"):
        document(tmp_path, "profiles: [broken]")


def test_file_changed_after_form_loaded_is_preserved(tmp_path):
    doc = document(tmp_path)
    text = edit_profile(doc, "p1", VALUES, KNOWN, TODAY)
    doc.path.write_text(PROFILE + "# external edit\n")
    with pytest.raises(EditorError, match="file_changed"):
        save_document(doc, text, (tmp_path,))
    assert doc.path.read_text().endswith("# external edit\n")


def test_new_file_created_elsewhere_is_not_overwritten(tmp_path):
    doc = document(tmp_path, None)
    text = edit_profile(doc, None, VALUES, KNOWN, TODAY)
    doc.path.write_text("external content")
    with pytest.raises(EditorError, match="file_changed"):
        save_document(doc, text, (tmp_path,))
    assert doc.path.read_text() == "external content"


def test_path_traversal_and_symlink_escape_are_refused(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "other.yaml"
    outside.write_text(PROFILE)
    for path in (root / ".." / "other.yaml", root / "link.yaml"):
        if path.name == "link.yaml":
            path.symlink_to(outside)
        with pytest.raises(EditorError, match="path_not_allowed"):
            read_document(path, (root,), KNOWN, TODAY)


def test_failed_new_file_save_removes_only_its_empty_directories(tmp_path, monkeypatch):
    path = tmp_path / "new" / "nested" / "profiles.yaml"
    doc = read_document(path, (tmp_path,), KNOWN, TODAY, create=True)
    text = edit_profile(doc, None, VALUES, KNOWN, TODAY)

    def denied(*args):
        raise PermissionError

    monkeypatch.setattr(profile_editor.os, "link", denied)
    with pytest.raises(EditorError, match="save_failed"):
        save_document(doc, text, (tmp_path,))
    assert not (tmp_path / "new").exists()


def test_malformed_path_is_a_validation_error(tmp_path):
    with pytest.raises(EditorError, match="path_not_allowed"):
        read_document(Path(str(tmp_path) + "/bad\x00name"), (tmp_path,), KNOWN, TODAY, create=True)


def test_failed_replacement_preserves_existing_file_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    doc = document(tmp_path)
    text = edit_profile(doc, "p1", VALUES, KNOWN, TODAY)

    def denied(*args):
        raise PermissionError

    monkeypatch.setattr(profile_editor.os, "replace", denied)
    with pytest.raises(EditorError, match="save_failed"):
        save_document(doc, text, (tmp_path,))
    assert doc.path.read_text() == PROFILE
    assert list(tmp_path.iterdir()) == [doc.path]


def test_edit_detaches_outbound_and_pauses_shared_by_two_profiles(tmp_path):
    raw = yaml.safe_load(PROFILE)
    first, second = raw["profiles"]
    first["days"], second["days"] = ["mon"], ["tue"]
    second["outbound"] = first["outbound"]
    first["pauses"] = [{"start": "2027-04-01", "end": "2027-04-07"}]
    second["pauses"] = first["pauses"]
    second["transfer_min"] = first["transfer_min"]
    original_second = copy.deepcopy(second)
    doc = document(tmp_path, yaml.safe_dump(raw))
    text = edit_profile(
        doc,
        "p1",
        {
            **VALUES,
            "days": ["mon"],
            "outbound_time": "09:12",
            "transfer_min": {"UT": 9},
            "pause_action": "add",
            "pause_start": "2027-05-01",
            "pause_end": "2027-05-07",
        },
        KNOWN,
        TODAY,
    )
    saved = yaml.safe_load(text)["profiles"]
    assert saved[0]["outbound"]["first_train"] == "09:12"
    assert saved[0]["transfer_min"]["UT"] == 9
    assert len(saved[0]["pauses"]) == 2
    assert saved[1] == original_second
    assert doc.data["profiles"][1] == original_second


def test_edit_detaches_aliased_outbound_and_return_journeys(tmp_path):
    raw = yaml.safe_load(PROFILE)
    first = raw["profiles"][0]
    first["days"] = []
    first["extra_dates"] = ["2026-01-01"]
    first["return"] = first["outbound"]
    raw["profiles"] = [first]
    doc = document(tmp_path, yaml.safe_dump(raw))
    text = edit_profile(doc, "p1", VALUES, KNOWN, TODAY)
    saved = yaml.safe_load(text)["profiles"][0]
    assert saved["outbound"]["first_train"] == "08:12"
    assert saved["return"]["first_train"] == "17:52"
    assert saved["outbound"]["transfers"] == ["UT"]
    assert saved["return"]["transfers"] == []


def test_profile_commit_requires_a_staged_file(tmp_path):
    from custom_components.train_commute_companion import profile_editor

    path = tmp_path / "profiles.yaml"
    document = profile_editor.ProfileDocument(path, None, {"profiles": []})
    transaction = profile_editor.ProfileWrite(document, path)
    with pytest.raises(EditorError, match="save_failed"):
        transaction.commit()
    assert not path.exists()


WINDOW_FORM = {
    **{k: v for k, v in VALUES.items() if k != "return_time"},
    "outbound_mode": "fixed",
    "outbound_pick": "earliest",
    "outbound_min_stay": 0,
    "return_mode": "window",
    "return_window_start": "15:00",
    "return_window_end": "20:00",
    "return_pick": "earliest_silent",
    "return_min_stay": 480.0,
}


def test_window_mode_saves_without_a_first_train_and_back_to_fixed(tmp_path):
    doc = document(tmp_path)
    text = edit_profile(doc, "p1", WINDOW_FORM, KNOWN, TODAY)
    saved = yaml.safe_load(text)["profiles"][0]
    assert saved["return"] | {"transfers": []} == {
        "mode": "window",
        "window": ["15:00", "20:00"],
        "pick": "earliest_silent",
        "min_stay_min": 480,
        "buffer_min": 5,
        "transfers": [],
    }
    assert "mode" not in saved["outbound"] and "pick" not in saved["outbound"]
    doc = document(tmp_path, text)
    back = {
        **WINDOW_FORM,
        "return_mode": "fixed",
        "return_time": "17:52",
        "return_pick": "earliest",
        "return_min_stay": 0,
    }
    del back["return_window_start"], back["return_window_end"]
    saved = yaml.safe_load(edit_profile(doc, "p1", back, KNOWN, TODAY))["profiles"][0]["return"]
    assert saved["mode"] == "fixed" and saved["first_train"] == "17:52"
    assert not {"window", "pick", "min_stay_min"} & saved.keys()


def test_window_mode_without_a_latest_time_is_refused(tmp_path):
    doc = document(tmp_path)
    form = {**WINDOW_FORM}
    del form["return_window_end"]
    with pytest.raises(EditorError) as err:
        edit_profile(doc, "p1", form, KNOWN, TODAY)
    assert "return.window[1]" in err.value.report


def test_a_fixed_profile_keeps_its_shape_through_the_new_form(tmp_path):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["return"]["window"] = ["16:52", "19:52"]
    doc = document(tmp_path, yaml.safe_dump(raw))
    form = {
        **VALUES,
        **{f"{d}_{k}": v for d in ("outbound", "return") for k, v in
           (("mode", "fixed"), ("pick", "earliest"), ("min_stay", 0))},
        "return_window_start": "16:52",
        "return_window_end": "19:52",
    }  # fmt: skip
    before = yaml.safe_load(edit_profile(doc, "p1", VALUES, KNOWN, TODAY))
    assert yaml.safe_load(edit_profile(doc, "p1", form, KNOWN, TODAY)) == before


def test_day_overrides_are_written_and_removed(tmp_path):
    doc = document(tmp_path)
    days = {"fri": {"mode": "window", "window": ["13:00", "18:00"]}}
    text = edit_profile(doc, "p1", {**VALUES, "return_by_weekday": days}, KNOWN, TODAY)
    profiles, errors = parse_profiles(text, KNOWN)
    assert not errors and profiles[0].return_.by_weekday["fri"].mode == "window"
    doc = document(tmp_path, text)
    text = edit_profile(doc, "p1", {**VALUES, "return_by_weekday": {}}, KNOWN, TODAY)
    assert "by_weekday" not in yaml.safe_load(text)["profiles"][0]["return"]


def test_other_stations_and_their_settings_are_written_only_when_set(tmp_path):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["location"] = {"source": "ha", "entity": "person.me"}
    doc = document(tmp_path, yaml.safe_dump(raw, sort_keys=False))
    untouched = {
        **VALUES,
        "outbound_other_stations": [],
        "return_other_stations": [],
        "other_station_radius_m": 500.0,
        "other_station_dwell_min": 0.0,
        "ask_to_remember_station": True,
    }
    same = yaml.safe_load(edit_profile(doc, "p1", untouched, KNOWN, TODAY))["profiles"][0]
    assert "other_stations" not in same["return"]
    assert same["location"] == {"source": "ha", "entity": "person.me"}
    changed = {
        **untouched,
        "return_other_stations": ["BBB"],
        "other_station_radius_m": 300.0,
        "ask_to_remember_station": False,
    }
    text = edit_profile(doc, "p1", changed, KNOWN, TODAY)
    (p, *_), errors = parse_profiles(text, KNOWN)
    assert not errors
    assert p.return_.other_stations == ("BBB",)
    assert p.location is not None
    assert (p.location.other_station_radius_m, p.location.ask_to_remember_station) == (300, False)
    assert isinstance(
        yaml.safe_load(text)["profiles"][0]["location"]["other_station_radius_m"], int
    )


def test_a_named_profile_is_edited_into_codes_without_touching_the_others(tmp_path):
    known = Stations({"AAA": ["Home"], "UT": ["Utrecht Centraal", "Utrecht"], "RTD": [], "BBB": []})
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0].update(home_station="Home", transfer_min={"Utrecht": 6})
    raw["profiles"][0]["outbound"]["transfers"] = ["Utrecht Centraal"]
    raw["profiles"][1]["home_station"] = "Home"
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(raw))
    doc = read_document(path, (tmp_path,), known, TODAY)
    changed = yaml.safe_load(
        edit_profile(doc, "p1", {**VALUES, "home_station": "AAA"}, known, TODAY)
    )
    assert changed["profiles"][0]["transfer_min"] == {"UT": 6}  # one key, the saved minimum kept
    assert changed["profiles"][1] == raw["profiles"][1]

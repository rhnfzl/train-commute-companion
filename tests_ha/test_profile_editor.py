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


def test_edit_preserves_id_and_all_advanced_fields(tmp_path):
    raw = yaml.safe_load(PROFILE)
    p = raw["profiles"][0]
    p.update(extra_dates=["2027-03-02"], pauses=[{"start": "2027-04-01", "end": "2027-04-07"}])
    p["alerts"] = {"quiet_hours": ["20:00", "06:00"]}
    p["return"].update(day_offset=1, window=["16:52", "19:52"])
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

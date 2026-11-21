from itertools import permutations
from unittest.mock import patch

import pytest
import voluptuous as vol
import yaml
from ha_rig import NOTIFY, PROFILE, fake_phone, profiles_path, write_profiles
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.train_commute_companion import validate
from custom_components.train_commute_companion.const import DOMAIN
from custom_components.train_commute_companion.profile_flow import ProfileEditorFlow
from train_commute_companion.model import Stations
from train_commute_companion.providers.base import ProviderError

CATALOGUE = Stations(
    {"AAA": ["Home station"], "UT": ["Utrecht Centraal", "Utrecht"], "RTD": ["Rotterdam Centraal"]}
)
CHECK = "custom_components.train_commute_companion.config_flow.check_key"
CATALOGUE_CALL = "custom_components.train_commute_companion.profile_flow.station_catalogue"
VALUES = {
    "name": "commute",
    "days": ["mon", "tue"],
    "home_station": "Home station",  # the single pickers send names, mapped to codes
    "work_station": "Rotterdam Centraal",
    "outbound_time": "08:12",
    "return_time": "17:52",
    "outbound_transfers": ["UT"],
    "return_transfers": [],
    "outbound_buffer": 10,
    "return_buffer": 5,
    "skip_public_holidays": True,
    "require_accessible": False,
    "disabled_categories": [],
}


@pytest.fixture(autouse=True)
def editor_config_directory(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


async def configure(hass, flow, values):
    return await hass.config_entries.flow.async_configure(flow["flow_id"], values)


async def details(hass, flow, **values):
    assert flow["step_id"] == "profile_details"
    return await configure(hass, flow, {"minimum_UT": 7, "pause_action": "keep", **values})


async def guided(hass, catalogue=CATALOGUE):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(catalogue)),
        patch(CATALOGUE_CALL, return_value=catalogue),
    ):
        return await configure(hass, flow, {"api_key": "k"})


async def test_guided_profile_waits_for_phone_confirmation_before_writing(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await guided(hass)
    assert flow["step_id"] == "profile"
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "home_station")
    # the value is the name: the closed picker shows its raw value
    assert selector.config["options"] == ["Home station", "Rotterdam Centraal", "Utrecht Centraal"]
    assert selector.config["custom_value"] is True  # what gives the picker its search box
    flow = await configure(
        hass, flow, {**VALUES, "require_accessible": True, "disabled_categories": ["crowding"]}
    )
    assert shown(flow)["custom_timetable_horizon"] is True
    assert shown(flow)["timetable_horizon_unit"] == "calendar_days"
    assert shown(flow)["timetable_horizon_count"] == 7
    flow = await details(
        hass, flow, pause_action="add", pause_start="2027-04-01", pause_end="2027-04-07"
    )
    assert flow["step_id"] == "notify" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    assert flow["step_id"] == "test" and not profiles_path(hass).exists()
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert flow["description"] == "guided"  # says where to add more commutes later
    raw = yaml.safe_load(profiles_path(hass).read_text())
    assert raw["profiles"][0]["name"] == "commute"
    assert raw["profiles"][0]["home_station"] == "AAA"
    assert raw["profiles"][0]["work_station"] == "RTD"
    assert raw["profiles"][0]["accessibility"] == {"require_accessible": True}
    assert raw["profiles"][0]["alerts"]["disabled_categories"] == ["crowding"]
    assert raw["profiles"][0]["transfer_min"]["UT"] == 7
    assert raw["profiles"][0]["pauses"] == [{"start": "2027-04-01", "end": "2027-04-07"}]


async def test_guided_setup_never_writes_over_an_existing_file(hass):
    write_profiles(hass, PROFILE)
    flow = await guided(hass)
    assert flow["step_id"] == "profiles_file"
    assert profiles_path(hass).read_text() == PROFILE


def station_keys(flow) -> dict:
    return {str(k): k for k in flow["data_schema"].schema if str(k) in STATION_FIELDS}


STATION_FIELDS = ("home_station", "work_station")


async def test_a_new_profile_starts_with_empty_stations(hass):
    """Required pick lists get their first item from HA; optional ones with no value stay
    empty, and the step itself asks for both stations."""
    flow = await guided(hass)
    keys = station_keys(flow)
    assert set(keys) == set(STATION_FIELDS)
    for key in keys.values():
        assert isinstance(key, vol.Optional) and key.default is vol.UNDEFINED
        assert not (key.description or {}).get("suggested_value")
    empty = {k: v for k, v in VALUES.items() if k not in STATION_FIELDS}
    flow = await configure(hass, flow, empty)
    assert flow["step_id"] == "profile"
    assert flow["errors"] == dict.fromkeys(STATION_FIELDS, "unknown_station")
    assert set(station_keys(flow)) == set(STATION_FIELDS)  # still there to be filled
    assert not profiles_path(hass).exists()


async def test_check_in_fields_are_optional_without_a_forced_default(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await guided(hass)
    for direction in ("outbound", "return"):
        key = next(k for k in flow["data_schema"].schema if str(k) == f"{direction}_active_lead")
        assert isinstance(key, vol.Optional) and key.default is vol.UNDEFINED
        assert not (key.description or {}).get("suggested_value")
    flow = await configure(
        hass, flow, {**VALUES, "outbound_active_lead": 40, "return_active_lead": 25}
    )
    assert flow["step_id"] == "profile_details"
    switch = next(k for k in flow["data_schema"].schema if str(k) == "leave_by")
    assert switch.default() is True
    flow = await details(hass, flow, leave_by=False)
    assert flow["step_id"] == "notify"
    assert not profiles_path(hass).exists()


@pytest.mark.parametrize("work", ["Home station", "home station", "AAA"])
async def test_home_and_work_must_differ(hass, work):
    flow = await configure(hass, await guided(hass), {**VALUES, "work_station": work})
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"work_station": "same_station"}
    assert not profiles_path(hass).exists()


async def test_an_older_same_station_profile_opens_and_can_be_fixed(hass):
    """Older versions saved home == work. The file still loads (a warning only), so the editor
    opens it: a healthy profile saves, and the old one is refused until its stations differ."""
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][1]["work_station"] = "AAA"  # office_b, on sat: no day clash with office_a
    write_profiles(hass, yaml.safe_dump(raw, sort_keys=False))
    assert validate.file_problem(str(profiles_path(hass))) == ""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)

    async def edit(profile_id):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        with patch(CATALOGUE_CALL, return_value=CATALOGUE):
            flow = await hass.config_entries.options.async_configure(
                flow["flow_id"], {"next_step_id": "profiles"}
            )
        assert flow["step_id"] == "profiles", flow.get("errors")
        return await hass.config_entries.options.async_configure(
            flow["flow_id"], {"profile": f"edit:{profile_id}"}
        )

    async def save(flow, values):
        flow = await hass.config_entries.options.async_configure(flow["flow_id"], values)
        assert flow["step_id"] == "profile_details", flow.get("errors")
        flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
        assert flow["reason"] == "profile_saved"

    flow = await edit("p1")
    await save(flow, {**shown(flow), "name": "office a renamed"})
    flow = await edit("p2")
    unchanged = shown(flow)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], unchanged)
    assert flow["errors"] == {"work_station": "same_station"}
    await save(flow, {**unchanged, "work_station": "Rotterdam Centraal"})
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"]
    assert [p["name"] for p in saved] == ["office a renamed", "office_b"]
    assert saved[1]["work_station"] == "RTD"


async def test_a_name_another_profile_has_is_refused_in_plain_words(hass):
    write_profiles(hass, PROFILE)
    flow = await options_add(hass)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "Office A", "days": ["sun"]}
    )
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"name": "name_taken"}
    assert flow["description_placeholders"]["report"] == ""
    assert profiles_path(hass).read_text() == PROFILE


async def options_add(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    return await hass.config_entries.options.async_configure(flow["flow_id"], {"profile": "new"})


async def test_add_a_profile_suggests_a_free_name_and_explains_a_day_clash(hass):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][1]["name"] = "commute"  # office_a runs mon-fri, commute on sat
    write_profiles(hass, yaml.safe_dump(raw))
    flow = await options_add(hass)
    assert flow["step_id"] == "profile"
    assert shown(flow)["name"] == "commute 2"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "commute 2", "days": ["tue", "sun"]}
    )
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"days": "days_clash"}
    assert flow["description_placeholders"]["other"] == "office_a"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "commute 2", "days": ["sun"]}
    )
    assert flow["step_id"] == "profile_details", flow.get("errors")


@pytest.mark.parametrize("field", ["home_station", "work_station"])
@pytest.mark.parametrize("typed", ["Nowhere", "Rotterdam", " "])
async def test_a_station_not_in_the_list_is_refused(hass, field, typed):
    flow = await configure(hass, await guided(hass), {**VALUES, field: typed})
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {field: "unknown_station"}
    assert shown(flow).get(field, " ") == typed  # the typed text stays to be fixed
    assert not profiles_path(hass).exists()


@pytest.mark.parametrize(
    "typed",
    ["rotterdam centraal", "Rotterdam Centraal ", " ROTTERDAM CENTRAAL", "Rotterdam C.", "RTD"],
)
async def test_a_typed_name_the_catalogue_knows_is_taken(hass, monkeypatch, typed):
    """Other case, extra spaces, a short name or the code all mean the one station."""
    fake_phone(hass, monkeypatch)
    catalogue = Stations({**CATALOGUE.labels, "RTD": ["Rotterdam Centraal", "Rotterdam C."]})
    flow = await configure(hass, await guided(hass, catalogue), {**VALUES, "work_station": typed})
    flow = await details(hass, flow)
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["work_station"] == "RTD"


@pytest.mark.parametrize("typed", ["Home station", "home station"])
async def test_a_name_two_stations_share_is_refused_when_typed(hass, typed):
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Home station"]})
    flow = await configure(hass, await guided(hass, catalogue), {**VALUES, "home_station": typed})
    assert flow["step_id"] == "profile"
    assert flow["errors"] == {"home_station": "unknown_station"}


COLLIDING = {"AAA": ["Home"], "BBB": ["Home"], "CCC": ["Home (AAA)"], "DDD": ["Home (AAA) (AAA)"]}


@pytest.mark.parametrize("order", list(permutations(COLLIDING)))
def test_every_station_keeps_its_own_label(order):
    """A real name that looks like a generated label never hides a station, in any order."""
    flow = ProfileEditorFlow()
    flow._catalogue = Stations({code: COLLIDING[code] for code in order})
    labels = flow._station_codes()
    assert sorted(labels.values()) == sorted(COLLIDING)  # one label per station
    assert labels["Home (AAA)"] == "CCC" and labels["Home (AAA) (AAA)"] == "DDD"
    assert all(flow._station_code(label) == code for label, code in labels.items())


async def test_an_existing_profile_with_a_colliding_label_opens_filled(hass):
    write_profiles(hass, PROFILE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    catalogue = Stations({**CATALOGUE.labels, **COLLIDING})
    with patch(CATALOGUE_CALL, return_value=catalogue):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    defaults = shown(flow)
    assert defaults["home_station"] == "Home (AAA) (AAA) (AAA)"  # AAA, never an empty field
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["step_id"] == "profile_details", flow.get("errors")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["reason"] == "profile_saved"
    assert yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["home_station"] == "AAA"


@pytest.mark.parametrize("typed", ["Home station (BBB)", " Home station (BBB) "])
async def test_two_stations_with_one_name_stay_apart(hass, typed):
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Home station"]})
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(catalogue)),
        patch(CATALOGUE_CALL, return_value=catalogue),
    ):
        flow = await configure(hass, flow, {"api_key": "k"})
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "home_station")
    assert {"Home station (AAA)", "Home station (BBB)"} <= set(selector.config["options"])
    flow = await configure(hass, flow, {**VALUES, "home_station": typed})
    assert flow["step_id"] == "profile_details", flow.get("errors")


async def test_guided_weekday_times_wait_for_validation_and_phone_confirmation(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await configure(hass, await guided(hass), VALUES)
    flow = await details(hass, flow, edit_weekday_times=True)
    assert flow["step_id"] == "profile_weekdays" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"outbound_tue": "25:00", "return_tue": "18:40"})
    assert flow["step_id"] == "profile_weekdays" and flow["errors"]["base"] == "invalid_profile"
    assert not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"outbound_tue": "09:12", "return_tue": "18:40"})
    assert flow["step_id"] == "notify" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]
    assert saved["outbound"]["first_train_by_weekday"] == {"tue": "09:12"}
    assert saved["return"]["first_train_by_weekday"] == {"tue": "18:40"}


async def test_reversed_holiday_remains_on_details_form_without_writing(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await configure(hass, await guided(hass), VALUES)
    flow = await details(
        hass, flow, pause_action="add", pause_start="2027-04-07", pause_end="2027-04-01"
    )
    assert flow["step_id"] == "profile_details"
    assert flow["errors"] == {"base": "invalid_profile"}
    assert "start" in flow["description_placeholders"]["report"]
    assert not profiles_path(hass).exists()


async def test_catalogue_failure_is_visible_and_does_not_write(hass):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(CATALOGUE)),
        patch(CATALOGUE_CALL, side_effect=ProviderError),
    ):
        flow = await configure(hass, flow, {"api_key": "k"})
    assert flow["errors"] == {"base": "cannot_connect"}
    assert not profiles_path(hass).exists()


async def test_an_empty_catalogue_is_a_clear_error_and_does_not_write(hass):
    flow = await guided(hass, Stations({}))
    assert flow["step_id"] == "profile_error"
    assert flow["errors"] == {"base": "cannot_connect"}
    assert not profiles_path(hass).exists()


async def test_options_profile_edit_preserves_advanced_settings(hass):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["pauses"] = [{"start": "2027-04-01", "end": "2027-04-07"}]
    raw["profiles"][0]["alerts"] = {
        "delay_min": 7,
        "delay_step_min": 4,
        "weather": False,
        "leave_by": False,
        "custom_timetable_horizon": False,
        "timetable_horizon_unit": "commute_dates",
        "timetable_horizon_count": 14,
    }
    write_profiles(hass, yaml.safe_dump(raw))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    assert flow["step_id"] == "profiles"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    assert flow["step_id"] == "profile"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**VALUES, "name": "renamed"}
    )
    assert flow["step_id"] == "profile_details"
    assert shown(flow)["delay_min"] == 7
    assert shown(flow)["delay_step_min"] == 4
    assert shown(flow)["custom_timetable_horizon"] is False
    assert shown(flow)["timetable_horizon_unit"] == "commute_dates"
    assert shown(flow)["timetable_horizon_count"] == 14
    count_selector = next(
        selector
        for field, selector in flow["data_schema"].schema.items()
        if field.schema == "timetable_horizon_count"
    )
    assert count_selector.serialize()["selector"]["number"]["max"] == 14
    for invalid in (True, False, "7", None, 7.5):
        with pytest.raises(vol.Invalid):
            count_selector(invalid)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "minimum_UT": 8,
            "pause_action": "keep",
            "delay_min": 10.0,
            "delay_step_min": 5.0,
            "custom_timetable_horizon": True,
            "timetable_horizon_unit": "calendar_days",
            "timetable_horizon_count": 10.0,
        },
    )
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())
    assert saved["profiles"][0]["id"] == "p1"
    assert saved["profiles"][0]["pauses"] == raw["profiles"][0]["pauses"]
    assert saved["profiles"][0]["alerts"] == {
        "delay_min": 10,
        "delay_step_min": 5,
        "disabled_categories": [],
        "weather": False,
        "leave_by": False,
        "custom_timetable_horizon": True,
        "timetable_horizon_unit": "calendar_days",
        "timetable_horizon_count": 10,
    }
    assert saved["profiles"][1] == raw["profiles"][1]


@pytest.mark.parametrize("webhooks", [False, True])
async def test_channel_form_only_offers_configured_destinations_and_saves_selection(hass, webhooks):
    raw = yaml.safe_load(PROFILE)
    if webhooks:
        raw["profiles"][0].setdefault("alerts", {})["channels"] = {
            "journey": ["primary", "webhook"]
        }
    write_profiles(hass, yaml.safe_dump(raw))
    options = {"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY}
    if webhooks:
        options["webhook_urls"] = ["https://example.test/hook"]
    entry = MockConfigEntry(domain=DOMAIN, data={"api_key": "k"}, options=options)
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], VALUES)
    selector = next(
        value for key, value in flow["data_schema"].schema.items() if str(key) == "channel_journey"
    )
    choices = {option["value"] for option in selector.config["options"]}
    assert choices == (
        {"default", "primary", "webhook", "primary_webhook"} if webhooks else {"default", "primary"}
    )
    if webhooks:
        field = next(k for k in flow["data_schema"].schema if str(k) == "channel_journey")
        assert field.default() == "primary_webhook"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "minimum_UT": 7,
            "pause_action": "keep",
            "channel_journey": "primary_webhook" if webhooks else "primary",
        },
    )
    assert flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())
    assert saved["profiles"][0]["alerts"]["channels"] == {
        "journey": ["primary", "webhook"] if webhooks else ["primary"]
    }


async def test_editor_rejects_new_file_created_during_notification_tests(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await guided(hass)
    flow = await configure(hass, flow, VALUES)
    flow = await details(hass, flow)
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    write_profiles(hass, PROFILE)
    flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["step_id"] == "profile_error"
    assert flow["errors"] == {"base": "file_changed"}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert profiles_path(hass).read_text() == PROFILE
    # retrying moves on to the path form with the new file filled in, not the same error
    flow = await configure(hass, flow, {})
    assert flow["step_id"] == "profiles_file" and not flow["errors"]
    path = next(k.default() for k in flow["data_schema"].schema if str(k) == "profiles_path")
    assert path == str(profiles_path(hass))
    flow = await configure(hass, flow, {"profiles_path": path})
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY and flow["description"] is None
    assert flow["options"]["profiles_path"] == path
    assert profiles_path(hass).read_text() == PROFILE  # the draft was dropped, never written


async def test_guided_window_profile_and_day_overrides_round_trip(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    step1 = {k: v for k, v in VALUES.items() if k != "return_time"}
    window = {"return_mode": "window", "return_window_start": "15:00"}
    flow = await configure(hass, await guided(hass), {**step1, **window})
    assert flow["step_id"] == "profile" and flow["errors"]["base"] == "invalid_profile"
    assert "return.window" in flow["description_placeholders"]["report"]
    window["return_window_end"] = "20:00"
    flow = await configure(hass, flow, {**step1, **window, "return_min_stay": 480})
    flow = await details(hass, flow, edit_day_overrides=True)
    assert flow["step_id"] == "profile_day_pick"
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "day")
    assert "return_fri" in {o["value"] for o in selector.config["options"]}
    flow = await configure(hass, flow, {"day": "return_fri"})
    assert flow["step_id"] == "profile_day"
    flow = await configure(hass, flow, {"mode": "fixed", "pick": "default"})
    assert flow["step_id"] == "profile_day" and flow["errors"]["base"] == "invalid_profile"
    flow = await configure(
        hass,
        flow,
        {
            "mode": "default",
            "window_start": "13:00",
            "window_end": "18:00",
            "min_stay": 300,
            "pick": "default",
        },
    )
    assert flow["step_id"] == "profile_day_pick"
    assert "return Friday" in flow["description_placeholders"]["overrides"]
    flow = await configure(hass, flow, {"day": "return_thu"})
    flow = await configure(hass, flow, {"mode": "default", "pick": "default", "min_stay": 0})
    assert flow["step_id"] == "profile_day_pick"  # 0 turns the stay off on Thursday
    flow = await configure(hass, flow, {"day": "done"})
    assert flow["step_id"] == "notify" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["return"]
    assert "first_train" not in saved
    assert saved["mode"] == "window" and saved["window"] == ["15:00", "20:00"]
    assert saved["min_stay_min"] == 480
    assert saved["by_weekday"] == {
        "fri": {"window": ["13:00", "18:00"], "min_stay_min": 300},
        "thu": {"min_stay_min": 0},
    }


def shown(flow) -> dict:
    """What the frontend sends back for an untouched form: suggested values and defaults."""
    out = {}
    for key in flow["data_schema"].schema:
        suggested = (key.description or {}).get("suggested_value")
        if suggested not in (None, ""):
            out[str(key)] = suggested
        elif not isinstance(key.default, type(vol.UNDEFINED)):
            out[str(key)] = key.default()
    return out


async def test_reopening_a_window_profile_saves_it_unchanged(hass):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["return"] = {
        "mode": "window",
        "window": ["15:00", "20:00"],
        "pick": "on_leave",
        "min_stay_min": 480,
        "buffer_min": 5,
        "transfers": [],
    }
    write_profiles(hass, yaml.safe_dump(raw))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    assert "return_time" not in shown(flow)  # blank: no first train is invented
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["step_id"] == "profile_details"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]["return"]
    assert saved == raw["profiles"][0]["return"]


async def test_editing_a_located_profile_shows_and_saves_other_stations_and_day_overrides(hass):
    """The options flow on a profile with a location block: the first step carries the window
    settings, the details step the other station settings, and a day override is saved too."""
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["location"] = {"source": "ha", "entity": "person.me"}
    write_profiles(hass, yaml.safe_dump(raw, sort_keys=False))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Other station"]})

    async def edit():
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        with patch(CATALOGUE_CALL, return_value=catalogue):
            flow = await hass.config_entries.options.async_configure(
                flow["flow_id"], {"next_step_id": "profiles"}
            )
        return await hass.config_entries.options.async_configure(
            flow["flow_id"], {"profile": "edit:p1"}
        )

    async def step(flow, values):
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    flow = await edit()
    assert flow["step_id"] == "profile"
    keys = {str(k) for k in flow["data_schema"].schema}
    for d in ("outbound", "return"):
        assert {
            f"{d}_{k}" for k in ("mode", "window_start", "window_end", "pick", "min_stay")
        } <= keys
    window = {
        "return_mode": "window",
        "return_window_start": "15:00",
        "return_window_end": "20:00",
        "return_pick": "earliest_silent",
        "return_min_stay": 480,
    }
    first = {k: v for k, v in shown(flow).items() if k != "return_time"}
    flow = await step(flow, {**first, **window})
    assert flow["step_id"] == "profile_details", flow.get("errors")
    defaults = shown(flow)
    assert {
        k: defaults[k]
        for k in (
            "outbound_other_stations",
            "return_other_stations",
            "other_station_radius_m",
            "other_station_dwell_min",
            "ask_to_remember_station",
            "edit_day_overrides",
        )
    } == {
        "outbound_other_stations": [],
        "return_other_stations": [],
        "other_station_radius_m": 500,
        "other_station_dwell_min": 0,
        "ask_to_remember_station": True,
        "edit_day_overrides": False,
    }
    selector = next(
        v for k, v in flow["data_schema"].schema.items() if str(k) == "return_other_stations"
    )
    assert "BBB" in {o["value"] for o in selector.config["options"]}
    flow = await step(
        flow,
        {
            **defaults,
            "return_other_stations": ["BBB"],
            "other_station_radius_m": 400.0,
            "other_station_dwell_min": 2.0,
            "ask_to_remember_station": False,
            "edit_day_overrides": True,
        },
    )
    assert flow["step_id"] == "profile_day_pick", flow.get("errors")
    flow = await step(flow, {"day": "return_fri"})
    assert flow["step_id"] == "profile_day"
    flow = await step(
        flow, {"mode": "default", "window_start": "13:00", "window_end": "18:00", "pick": "default"}
    )
    assert flow["step_id"] == "profile_day_pick", flow.get("errors")
    flow = await step(flow, {"day": "done"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"

    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]
    assert saved["location"] == {
        "source": "ha",
        "entity": "person.me",
        "other_station_radius_m": 400,
        "other_station_dwell_min": 2,
        "ask_to_remember_station": False,
    }
    assert saved["return"]["other_stations"] == ["BBB"]
    assert "other_stations" not in saved["outbound"]
    assert saved["return"]["mode"] == "window" and saved["return"]["window"] == ["15:00", "20:00"]
    assert saved["return"]["pick"] == "earliest_silent" and saved["return"]["min_stay_min"] == 480
    assert saved["return"]["by_weekday"] == {"fri": {"window": ["13:00", "18:00"]}}

    flow = await edit()  # reopened, the saved settings are what the forms show
    assert {k: shown(flow).get(k) for k in window} == window
    flow = await step(flow, shown(flow))
    again = shown(flow)
    assert again["return_other_stations"] == ["BBB"]
    assert again["other_station_radius_m"] == 400 and again["other_station_dwell_min"] == 2
    assert again["ask_to_remember_station"] is False
    flow = await step(flow, {**again, "edit_day_overrides": True})
    assert flow["step_id"] == "profile_day_pick"
    assert "return Friday" in flow["description_placeholders"]["overrides"]


async def test_a_profile_that_names_its_stations_opens_filled_and_saves_codes(hass):
    """Names fill the station fields; the edited profile is saved with codes and one
    transfer minimum per station, and the other profile keeps its names."""
    raw = yaml.safe_load(PROFILE)
    first, second = raw["profiles"]
    first.update(home_station="Home station", transfer_min={"Utrecht": 9})
    first["outbound"]["transfers"] = ["Utrecht Centraal"]
    first["location"] = {"source": "ha", "entity": "person.me"}
    first["return"]["other_stations"] = ["Other station"]
    second["work_station"] = "Rotterdam Centraal"
    write_profiles(hass, yaml.safe_dump(raw, sort_keys=False))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    catalogue = Stations({**CATALOGUE.labels, "BBB": ["Other station"]})
    with patch(CATALOGUE_CALL, return_value=catalogue):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    defaults = shown(flow)
    assert (defaults["home_station"], defaults["outbound_transfers"]) == ("Home station", ["UT"])
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["step_id"] == "profile_details", flow.get("errors")
    defaults = shown(flow)
    assert defaults["minimum_UT"] == 9 and defaults["return_other_stations"] == ["BBB"]
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"]
    assert saved[0]["home_station"] == "AAA" and saved[0]["transfer_min"] == {"UT": 9}
    assert saved[0]["outbound"]["transfers"] == ["UT"]
    assert saved[0]["return"]["other_stations"] == ["BBB"]
    assert saved[1] == second


async def test_an_old_profile_stored_with_codes_opens_with_names_and_saves_codes(hass):
    write_profiles(hass, PROFILE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={"profiles_path": str(profiles_path(hass)), "notify_service": NOTIFY},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(CATALOGUE_CALL, return_value=CATALOGUE):
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "profiles"}
        )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"profile": "edit:p1"}
    )
    defaults = shown(flow)
    assert (defaults["home_station"], defaults["work_station"]) == (
        "Home station",
        "Rotterdam Centraal",
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], defaults)
    assert flow["step_id"] == "profile_details", flow.get("errors")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], shown(flow))
    assert flow["reason"] == "profile_saved"
    before = yaml.safe_load(PROFILE)["profiles"][0]
    saved = yaml.safe_load(profiles_path(hass).read_text())["profiles"][0]
    assert (saved["home_station"], saved["work_station"]) == ("AAA", "RTD")
    assert saved["outbound"]["transfers"] == before["outbound"]["transfers"]

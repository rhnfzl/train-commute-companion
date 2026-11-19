from unittest.mock import patch

import pytest
import voluptuous as vol
import yaml
from ha_rig import NOTIFY, PROFILE, fake_phone, profiles_path, write_profiles
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.train_commute_companion.const import DOMAIN
from train_commute_companion.providers.base import ProviderError

CATALOGUE = {"AAA": "Home station", "UT": "Utrecht Centraal", "RTD": "Rotterdam Centraal"}
CHECK = "custom_components.train_commute_companion.config_flow.check_key"
CATALOGUE_CALL = "custom_components.train_commute_companion.profile_flow.station_catalogue"
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


async def guided(hass):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(CATALOGUE)),
        patch(CATALOGUE_CALL, return_value=CATALOGUE),
    ):
        return await configure(
            hass,
            flow,
            {"api_key": "k", "profiles_path": str(profiles_path(hass)), "guided_setup": True},
        )


async def test_guided_profile_waits_for_phone_confirmation_before_writing(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = await guided(hass)
    assert flow["step_id"] == "profile"
    selector = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "home_station")
    assert {o["label"] for o in selector.config["options"]} == set(CATALOGUE.values())
    flow = await configure(
        hass, flow, {**VALUES, "require_accessible": True, "disabled_categories": ["crowding"]}
    )
    flow = await details(
        hass, flow, pause_action="add", pause_start="2027-04-01", pause_end="2027-04-07"
    )
    assert flow["step_id"] == "notify" and not profiles_path(hass).exists()
    flow = await configure(hass, flow, {"notify_service": NOTIFY})
    assert flow["step_id"] == "test" and not profiles_path(hass).exists()
    with patch("custom_components.train_commute_companion.async_setup_entry", return_value=True):
        flow = await configure(hass, flow, {"next_step_id": "test_ok"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    raw = yaml.safe_load(profiles_path(hass).read_text())
    assert raw["profiles"][0]["name"] == "commute"
    assert raw["profiles"][0]["home_station"] == "AAA"
    assert raw["profiles"][0]["accessibility"] == {"require_accessible": True}
    assert raw["profiles"][0]["alerts"]["disabled_categories"] == ["crowding"]
    assert raw["profiles"][0]["transfer_min"]["UT"] == 7
    assert raw["profiles"][0]["pauses"] == [{"start": "2027-04-01", "end": "2027-04-07"}]


async def test_guided_setup_refuses_existing_file(hass):
    write_profiles(hass, PROFILE)
    flow = await guided(hass)
    assert flow["step_id"] == "profile_error"
    assert flow["errors"] == {"base": "file_changed"}
    assert profiles_path(hass).read_text() == PROFILE


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


async def test_guided_setup_refuses_path_outside_config_directory(hass, tmp_path):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with (
        patch(CHECK, return_value=frozenset(CATALOGUE)),
        patch(CATALOGUE_CALL, return_value=CATALOGUE),
    ):
        flow = await configure(
            hass,
            flow,
            {
                "api_key": "k",
                "profiles_path": str(tmp_path.parent / "outside.yaml"),
                "guided_setup": True,
            },
        )
    assert flow["errors"] == {"base": "path_not_allowed"}
    assert not (tmp_path.parent / "outside.yaml").exists()


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
        flow = await configure(
            hass,
            flow,
            {"api_key": "k", "profiles_path": str(profiles_path(hass)), "guided_setup": True},
        )
    assert flow["errors"] == {"base": "cannot_connect"}
    assert not profiles_path(hass).exists()


async def test_options_profile_edit_preserves_advanced_settings(hass):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"][0]["pauses"] = [{"start": "2027-04-01", "end": "2027-04-07"}]
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
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"minimum_UT": 8, "pause_action": "keep"}
    )
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())
    assert saved["profiles"][0]["id"] == "p1"
    assert saved["profiles"][0]["pauses"] == raw["profiles"][0]["pauses"]
    assert saved["profiles"][1] == raw["profiles"][1]


@pytest.mark.parametrize("webhooks", [False, True])
async def test_channel_form_only_offers_configured_destinations_and_saves_selection(hass, webhooks):
    write_profiles(hass, PROFILE)
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
        {"default", "primary", "webhook", "primary,webhook"} if webhooks else {"default", "primary"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "minimum_UT": 7,
            "pause_action": "keep",
            "channel_journey": "webhook" if webhooks else "primary",
        },
    )
    assert flow["reason"] == "profile_saved"
    saved = yaml.safe_load(profiles_path(hass).read_text())
    assert saved["profiles"][0]["alerts"]["channels"] == {
        "journey": ["webhook" if webhooks else "primary"]
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
    flow = await configure(hass, flow, {"day": "return:fri"})
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
    flow = await configure(hass, flow, {"day": "return:thu"})
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
    catalogue = {**CATALOGUE, "BBB": "Other station"}

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
    flow = await step(flow, {"day": "return:fri"})
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

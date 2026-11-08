from unittest.mock import patch

import pytest
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

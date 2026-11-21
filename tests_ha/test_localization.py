import json
import re
from pathlib import Path
from string import Formatter

import pytest
from ha_rig import PROFILE, setup_tcc
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from test_profile_setup import VALUES, configure, guided

from custom_components.train_commute_companion.const import DOMAIN

TRANSLATIONS = Path(__file__).parents[1] / "custom_components" / DOMAIN / "translations"


@pytest.mark.parametrize(
    "catalog", ["strings.json", "translations/en.json", "translations/nl.json"]
)
def test_selector_translation_keys_meet_hassfest_contract(catalog):
    data = json.loads((TRANSLATIONS.parent / catalog).read_text())
    for selector in data["selector"].values():
        for key in selector["options"]:
            assert re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?", key), key


async def test_home_assistant_loads_dutch_setup_and_options(hass):
    config = await async_get_translations(hass, "nl", "config", {DOMAIN})
    options = await async_get_translations(hass, "nl", "options", {DOMAIN})
    selector = await async_get_translations(hass, "nl", "selector", {DOMAIN})
    prefix = f"component.{DOMAIN}"
    assert config[f"{prefix}.config.step.user.data.api_key"] == "NS API-sleutel"
    assert config[f"{prefix}.config.step.profile.data.home_station"] == "Station bij huis"
    assert (
        options[f"{prefix}.options.step.init.menu_options.profiles"]
        == "Profielen toevoegen of wijzigen"
    )
    assert selector[f"{prefix}.selector.loud_style.options.critical"].startswith("Kritiek:")


def test_dutch_forms_cover_english_fields_and_preserve_placeholders():
    english = json.loads((TRANSLATIONS / "en.json").read_text())
    dutch = json.loads((TRANSLATIONS / "nl.json").read_text())

    def check(source, translated):
        if isinstance(source, dict):
            assert translated.keys() == source.keys()
            for key in source:
                check(source[key], translated[key])
        else:
            placeholders = lambda text: sorted(
                field for _, field, _, _ in Formatter().parse(text) if field is not None
            )
            assert translated.strip()
            assert placeholders(source) == placeholders(translated)

    for section in ("config", "options", "selector"):
        check(english[section], dutch[section])


async def test_profile_choices_use_localized_labels_without_changing_values(hass):
    flow = await guided(hass)
    schema = {str(key): value for key, value in flow["data_schema"].schema.items()}
    for field, key in {
        "days": "weekdays",
        "disabled_categories": "alert_categories",
        "outbound_mode": "journey_mode",
        "return_mode": "journey_mode",
        "outbound_pick": "journey_pick",
        "return_pick": "journey_pick",
    }.items():
        assert schema[field].config["translation_key"] == key
    assert schema["outbound_mode"]("fixed") == "fixed"
    flow = await configure(hass, flow, VALUES)
    schema = {str(key): value for key, value in flow["data_schema"].schema.items()}
    assert schema["pause_action"].config["translation_key"] == "holiday_ranges"
    assert schema["channel_journey"].config["translation_key"] == "notification_destinations"
    assert schema["pause_action"]("keep") == "keep"


@pytest.mark.usefixtures("tcc_freezer")
@pytest.mark.parametrize("language", ["en", "nl"])
async def test_releases_keep_saved_entity_ids_in_both_languages(hass, language):
    hass.config.language = language
    profile = PROFILE.split('  - id: "p2"')[0]
    entry = (await setup_tcc(hass, profile)).entry
    expected = {
        "p1_health",
        "p1_not_travelling_today",
        "p1_outbound_status",
        "p1_outbound_lock",
        "p1_outbound_next_connection",
        "p1_outbound_advice",
        "p1_outbound_works_ahead",
        "p1_outbound_leg_1",
        "p1_outbound_leg_2",
        "p1_outbound_leg_3",
        "p1_outbound_transfer_1",
        "p1_outbound_transfer_2",
        "p1_outbound_leaving",
        "p1_outbound_not_travelling",
        "p1_outbound_choose_next",
        "p1_return_status",
        "p1_return_lock",
        "p1_return_next_connection",
        "p1_return_advice",
        "p1_return_works_ahead",
        "p1_return_leg_1",
        "p1_return_leg_2",
        "p1_return_transfer_1",
        "p1_return_leaving",
        "p1_return_not_travelling",
        "p1_return_choose_next",
    }
    registry = er.async_get(hass)
    before = {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert set(before) == expected
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after = {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before

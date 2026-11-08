from pathlib import Path

import yaml
from ha_rig import PROFILE, drive, setup_tcc
from homeassistant.helpers.template import Template


def dashboard():
    return yaml.safe_load((Path(__file__).parents[1] / "examples/dashboard.yaml").read_text())


def render(hass):
    content = dashboard()["cards"][0]["content"]
    return Template(content, hass).async_render(parse_result=False)


async def test_dashboard_shows_journey_attributes_and_existing_actions(hass, tcc_freezer):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:00")

    result = render(hass)
    assert "1234" in result and "AAA" in result and "UT" in result
    assert "delay: 5 min" in result
    assert "margin: 11 min" in result
    assert "minimum: 4 min" in result
    assert "No journey is being followed" not in result
    for card in dashboard()["cards"][1]["cards"]:
        assert hass.states.get(card["entity"]) is not None
        assert card["tap_action"]["target"]["entity_id"] == card["entity"]


async def test_dashboard_reports_unknown_without_entities(hass):
    result = render(hass)
    assert "Data is not verified" in result
    assert "Unknown" in result
    assert "No journey is being followed" not in result


async def test_dashboard_handles_empty_missing_and_stale_attributes(hass):
    hass.states.async_set("sensor.office_a_health", "cannot_see")
    hass.states.async_set("sensor.office_a_outbound_status", "locked")
    hass.states.async_set("sensor.office_a_outbound_leg_1", "delayed", {"train_number": "1234"})
    hass.states.async_set("sensor.office_a_outbound_transfer_1", "at_risk")
    hass.states.async_set("sensor.office_a_outbound_works_ahead", "0")

    result = render(hass)
    assert "Data is not verified" in result
    assert "1234" in result
    assert "margin: Unknown" in result
    assert "minimum: Unknown" in result
    assert "Last successful NS answer: Unknown" in result
    assert "Last works check: Unknown" in result

    hass.states.async_set("sensor.office_a_health", "idle")
    hass.states.async_set("sensor.office_a_outbound_status", "none")
    hass.states.async_set("sensor.office_a_outbound_leg_1", "none")
    result = render(hass)
    assert "No journey is being followed" in result
    assert "1234" not in result

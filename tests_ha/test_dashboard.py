import pytest
from ha_rig import PROFILE, drive, setup_tcc
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template

from custom_components.train_commute_companion.const import DOMAIN

pytestmark = pytest.mark.usefixtures("tcc_freezer")


async def dashboard(hass, profile="office_a", direction="outbound"):
    return await hass.services.async_call(
        DOMAIN,
        "dashboard_card",
        {"profile": profile, "direction": direction},
        blocking=True,
        return_response=True,
    )


def render(hass, card):
    content = card["cards"][0]["content"]
    return Template(content, hass).async_render(parse_result=False)


async def test_dashboard_shows_journey_attributes_and_existing_actions(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:00")
    card = await dashboard(hass)

    result = render(hass, card)
    assert "1234" in result and "AAA" in result and "UT" in result
    assert "delay: 5 min" in result
    assert "margin: 11 min" in result
    assert "minimum: 4 min" in result
    assert "No journey is being followed" not in result
    for button in card["cards"][1]["cards"]:
        assert hass.states.get(button["entity"]) is not None
        assert button["tap_action"]["target"]["entity_id"] == button["entity"]


async def test_dashboard_uses_registry_ids_for_renamed_entities(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:00")
    registry = er.async_get(hass)
    for old, new in (
        ("sensor.office_a_outbound_status", "sensor.my_journey"),
        ("sensor.office_a_outbound_leg_1", "sensor.my_first_train"),
        ("button.office_a_outbound_leaving", "button.leave_for_train"),
    ):
        registry.async_update_entity(old, new_entity_id=new)
    await hass.async_block_till_done()

    card = await dashboard(hass, profile="p1")
    result = render(hass, card)
    assert "1234" in result and "Journey:" in result
    assert card["cards"][1]["cards"][0]["entity"] == "button.leave_for_train"
    returned = await dashboard(hass, direction="return")
    assert returned["cards"][1]["cards"][0]["entity"] == "button.office_a_return_leaving"
    assert "1234" not in render(hass, returned)


async def test_dashboard_reports_missing_data_and_adds_recovery_legs(hass):
    h = await setup_tcc(hass, PROFILE)
    card = await dashboard(hass)
    hass.states.async_set("sensor.office_a_health", "cannot_see")
    hass.states.async_set("sensor.office_a_outbound_status", "locked")
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "p1"), h.entry.entry_id)
    assert device is not None
    new = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "p1_outbound_leg_7", config_entry=h.entry, device_id=device.id
    )
    hass.states.async_set(
        new.entity_id,
        "delayed",
        {"train_number": "9999", "commute_card_group": "outbound_leg", "commute_card_index": 7},
    )

    result = render(hass, card)
    assert "Data is not verified" in result
    assert "9999" in result
    assert "Last successful NS answer: Unknown" in result
    hass.states.async_set("sensor.office_a_outbound_status", "none")
    result = render(hass, card)
    assert "No journey is being followed" in result
    assert "9999" not in result


async def test_dashboard_rejects_unknown_profile(hass):
    await setup_tcc(hass, PROFILE)
    with pytest.raises(ServiceValidationError, match="unknown profile"):
        await dashboard(hass, profile="missing")

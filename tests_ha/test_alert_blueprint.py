from pathlib import Path
from shutil import copyfile

import pytest
from homeassistant.setup import async_setup_component

from custom_components.train_commute_companion.const import EVENT_ALERT

BLUEPRINT = Path("custom_components/train_commute_companion/blueprints/loud_alert.yaml")


@pytest.mark.parametrize("flash", [False, True])
@pytest.mark.parametrize("speak", [False, True])
async def test_blueprint_runs_only_for_a_loud_alert_in_the_selected_profile(hass, flash, speak):
    path = Path(hass.config.path("blueprints", "automation", BLUEPRINT.name))
    path.parent.mkdir(parents=True)
    copyfile(BLUEPRINT, path)
    inputs = {"profile_id": "p1"}
    if flash:
        inputs["lights"] = ["light.hall"]
    if speak:
        inputs |= {
            "speaker": "media_player.kitchen",
            "speech_engine": "tts.house",
        }
    calls = []
    for domain, action in (("light", "turn_on"), ("tts", "speak")):
        hass.services.async_register(domain, action, calls.append)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {"alias": "Commute relay", "use_blueprint": {"path": path.name, "input": inputs}}
            ]
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get("automation.commute_relay") is not None
    for profile, severity in (("p1", "silent"), ("p2", "loud"), ("p1", "loud")):
        hass.bus.async_fire(
            EVENT_ALERT, {"profile_id": profile, "severity": severity, "text": "Private trip"}
        )
        await hass.async_block_till_done()
        if profile != "p1" or severity != "loud":
            assert not calls
    expected = [("light", "turn_on")] if flash else []
    if speak:
        expected.append(("tts", "speak"))
    assert [(call.domain, call.service) for call in calls] == expected
    if flash:
        assert calls[0].data["entity_id"] == ["light.hall"]
        assert calls[0].data["flash"] == "short"
    if speak:
        assert calls[-1].data["media_player_entity_id"] == "media_player.kitchen"
        assert "Private trip" not in calls[-1].data["message"]

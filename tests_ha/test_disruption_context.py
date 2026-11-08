import json

from ha_rig import PROFILE, setup_tcc

from train_commute_companion.providers.base import ActiveDisruption, ActiveDisruptions


async def test_ha_context_attributes_are_json_serializable_and_keep_coverage(hass, tcc_freezer):
    h = await setup_tcc(hass, PROFILE)
    h.provider.disruptions = ActiveDisruptions(
        (ActiveDisruption("42", "A signal fault.", frozenset({"UT"}), None),), "complete"
    )
    await h.host.run(lambda rt: rt.poll_once())
    await h.entry.runtime_data.coordinator.async_refresh()
    attributes = hass.states.get("sensor.office_a_outbound_status").attributes
    assert attributes["active_disruptions"][0]["title"] == "A signal fault."
    assert attributes["disruptions_coverage"] == "complete"
    assert attributes["disruptions_checked_at"] is not None
    json.dumps(attributes["active_disruptions"])

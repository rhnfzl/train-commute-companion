import json

import ha_rig
from ha_rig import PROFILE, setup_tcc
from vehicle_rig import ContextProvider


async def test_ha_status_exposes_context_without_measurement_or_accessibility_guarantees(
    hass, tcc_freezer, monkeypatch
):
    monkeypatch.setattr(ha_rig, "HaProvider", ContextProvider)
    harness = await setup_tcc(hass, PROFILE)
    await harness.host.run(lambda rt: rt.vehicle_context_idle())
    await harness.entry.runtime_data.coordinator.async_refresh()
    attributes = hass.states.get("sensor.office_a_outbound_status").attributes
    context = attributes["train_context"]
    assert context["context"]["train_type"] == "VIRM"
    assert context["context"]["station"] == "AAA"
    assert context["context"]["units"][0]["capacity"]["first_class"] == 10
    assert context["measurement_at"] is None and context["operating_date_confirmed"] is False
    assert context["equipment_operation_confirmed"] is False
    json.dumps(context)

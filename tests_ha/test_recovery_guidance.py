import json

import yaml
from ha_rig import PROFILE, jump, setup_tcc
from recovery_sample import answer

from train_commute_companion.recovery_state import RecoveryOffer, SelectedRecovery


async def test_ha_status_exposes_serializable_physical_replacement_guidance(hass, tcc_freezer):
    raw = yaml.safe_load(PROFILE)
    raw["profiles"] = [raw["profiles"][0]]
    profile = raw["profiles"][0]
    profile.update(days=["sat"], home_station="UT", skip_public_holidays=False)
    profile["outbound"].update(first_train="11:58", transfers=[], buffer_min=30)
    h = await setup_tcc(
        hass,
        yaml.safe_dump(raw),
        now="11:30",
        world=answer().connections,
    )
    await jump(h, "2026-11-07 11:30")
    await h.host.run(lambda rt: rt.poll_once())
    await h.entry.runtime_data.coordinator.async_refresh()
    state = hass.states.get("sensor.office_a_outbound_status")
    assert state is not None, (
        h.entry.runtime_data.coordinator.data.refusal,
        list(hass.states.async_entity_ids()),
    )
    attributes = state.attributes
    plans = attributes["replacement_plans"]
    assert len(plans) == 1
    itinerary = plans[0]["itinerary"]
    assert itinerary["followed"] is False and itinerary["origin_confirmed"] is False
    assert itinerary["steps"][1]["mode"] == "WALK"
    assert itinerary["steps"][2]["departure_provenance"] == "planned"
    assert itinerary["transfers"][0]["walking_seconds"] == 300
    json.dumps(plans)

    shown = next(
        o
        for o in h.entry.runtime_data.coordinator.data.occurrences
        if o.key.direction.value == "outbound"
    )
    origin = shown.replacement_plans[0].itinerary.steps[0].origin.physical_id
    offered = await h.host.run(
        lambda rt: rt.prepare_recovery(shown.key, plans[0]["proposal_id"], origin)
    )
    assert isinstance(offered, RecoveryOffer)
    selected = await h.host.run(
        lambda rt: rt.select_recovery(
            shown.key,
            offered.id,
            acknowledge_planned_times=True,
            acknowledge_accessibility_unknown=True,
        )
    )
    assert isinstance(selected, SelectedRecovery)
    await h.entry.runtime_data.coordinator.async_refresh()
    active = hass.states.get("sensor.office_a_outbound_status")
    assert active.state == "recovery_active"
    assert active.attributes["original_train_state"] != "recovery_active"
    assert active.attributes["selected_recovery"]["id"] == selected.id
    assert (
        active.attributes["selected_recovery"]["progress_source"] == "explicit user confirmations"
    )
    assert "source_query" not in active.attributes["selected_recovery"]
    json.dumps(active.attributes["selected_recovery"])

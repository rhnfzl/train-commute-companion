from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from ha_rig import jump, profiles_path, setup_tcc, state_path
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from recovery_sample import answer

from custom_components.train_commute_companion.const import DOMAIN
from custom_components.train_commute_companion.recovery_button import RecoveryButton, intents
from train_commute_companion.model import Direction
from train_commute_companion.store import StorageError

pytestmark = pytest.mark.usefixtures("tcc_freezer")

PROFILE = """profiles:
  - id: p1
    name: office
    days: [sat]
    home_station: UT
    work_station: RTD
    outbound: {first_train: '11:58', transfers: [], buffer_min: 5, window: ['10:30', '16:00']}
    return: {first_train: '19:00', buffer_min: 5}
"""


async def world(hass):
    h = await setup_tcc(hass, PROFILE, now="11:30", world=answer().connections)
    await jump(h, "2026-11-07 11:30")
    await h.host.run(lambda rt: rt.poll_once())
    await settle(h)
    return h


async def settle(h):
    await h.entry.runtime_data.coordinator.async_refresh()
    await h.hass.async_block_till_done()


def shown(h):
    return next(
        o
        for o in h.entry.runtime_data.coordinator.data.occurrences
        if o.key.direction is Direction.OUTBOUND
    )


async def configure(h, flow, values):
    # Submit the reviewed selector value as the frontend does; negative token tests bypass this helper.
    for field, selector in getattr(flow.get("data_schema"), "schema", {}).items():
        if str(field) == "intent_id" and "intent_id" not in values:
            values = {**values, "intent_id": selector.config["options"][0]["value"]}
    return await h.hass.config_entries.options.async_configure(flow["flow_id"], values)


async def open_recovery(h):
    flow = await h.hass.config_entries.options.async_init(h.entry.entry_id)
    assert "recovery" in flow["menu_options"]
    return await configure(h, flow, {"next_step_id": "recovery"})


async def prepare_flow(h):
    plan = shown(h).replacement_plans[0]
    origin = next(step.origin for step in plan.itinerary.steps if step.mode == "BUS")
    flow = await configure(h, await open_recovery(h), {"choice": plan.proposal_id})
    assert flow["step_id"] == "recovery_origin"
    selector = next(
        value for key, value in flow["data_schema"].schema.items() if str(key) == "origin_place_id"
    )
    assert any(origin.name in option["label"] for option in selector.config["options"])
    flow = await configure(
        h, flow, {"origin_place_id": origin.physical_id, "confirm_location": True}
    )
    assert flow["step_id"] == "recovery_offer"
    assert origin.name in flow["description_placeholders"]["itinerary"]
    return flow


async def follow_flow(h):
    flow = await prepare_flow(h)
    flow = await configure(
        h,
        flow,
        {
            "confirm_location": True,
            "planned_times": True,
            "accessibility_unknown": True,
        },
    )
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "recovery_selected"
    await settle(h)
    return shown(h).selected_recovery


def base(h):
    key = shown(h).key
    return {
        "profile": key.profile_id,
        "direction": key.direction.value,
        "service_date": key.service_date.isoformat(),
    }


async def call(h, service, values):
    result = await h.hass.services.async_call(
        DOMAIN, service, {**base(h), **values}, blocking=True, return_response=True
    )
    await settle(h)
    return result


async def test_options_requires_location_and_both_limits_without_profile_save_or_reload(hass):
    h = await world(hass)
    original = profiles_path(hass).read_bytes()
    options = dict(h.entry.options)
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        flow = await prepare_flow(h)
        refused = await configure(
            h,
            flow,
            {"confirm_location": True, "planned_times": False, "accessibility_unknown": True},
        )
        assert refused["errors"] == {"base": "recovery_acknowledgement"}
        assert shown(h).selected_recovery is None
        success = await configure(
            h,
            refused,
            {"confirm_location": True, "planned_times": True, "accessibility_unknown": True},
        )
        assert success["reason"] == "recovery_selected"
        await settle(h)
        assert shown(h).selected_recovery.itinerary.steps[0].mode == "BUS"
        assert profiles_path(hass).read_bytes() == original and dict(h.entry.options) == options
        assert not reload.called


async def test_location_and_accessibility_acknowledgements_are_required_before_action(hass):
    h = await world(hass)
    plan = shown(h).replacement_plans[0]
    origin = next(step.origin for step in plan.itinerary.steps if step.mode == "BUS")
    flow = await configure(h, await open_recovery(h), {"choice": plan.proposal_id})
    requests = await h.host.run(lambda rt: rt.requests)
    flow = await configure(
        h, flow, {"origin_place_id": origin.physical_id, "confirm_location": False}
    )
    assert flow["errors"]["base"] == "recovery_acknowledgement"
    assert await h.host.run(lambda rt: rt.requests) == requests
    flow = await configure(
        h, flow, {"origin_place_id": origin.physical_id, "confirm_location": True}
    )
    for values in (
        {"confirm_location": False, "planned_times": True, "accessibility_unknown": True},
        {"confirm_location": True, "planned_times": True, "accessibility_unknown": False},
    ):
        flow = await configure(h, flow, values)
        assert flow["errors"]["base"] == "recovery_acknowledgement"
        assert shown(h).selected_recovery is None


async def test_bound_buttons_complete_bus_walk_metro_without_clock_progress(hass):
    h = await world(hass)
    selected = await follow_flow(h)
    while selected.status == "active":
        intent = intents(selected)[0]
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("button", DOMAIN, "recovery_" + intent.id)
        assert entity_id and intent.id in entity_id
        attributes = hass.states.get(entity_id).attributes
        assert attributes["place_id"] == intent.place_id
        assert attributes["latitude"] == intent.latitude
        assert attributes["longitude"] == intent.longitude
        step = selected.itinerary.steps[selected.index]
        at = step.departure if intent.kind == "boarded" else step.arrival
        await jump(h, at.strftime("%Y-%m-%d %H:%M"))
        await settle(h)
        assert shown(h).selected_recovery.confirmations == selected.confirmations
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
        await settle(h)
        updated = shown(h).selected_recovery
        assert len(updated.confirmations) == len(selected.confirmations) + 1
        assert updated.confirmations[-1].kind == intent.kind
        assert updated.confirmations[-1].place.physical_id == intent.place_id
        assert registry.async_get_entity_id("button", DOMAIN, "recovery_" + intent.id) is None
        selected = updated
    assert selected.status == "completed"


async def test_stale_button_and_options_action_cannot_confirm_the_next_step(hass):
    h = await world(hass)
    selected = await follow_flow(h)
    intent = intents(selected)[0]
    button = RecoveryButton(
        h.entry.runtime_data.coordinator, h.entry.runtime_data, "office", intent
    )
    flow = await configure(h, await open_recovery(h), {"choice": intent.id})
    assert flow["step_id"] == "recovery_action"
    assert intent.place_id in flow["description_placeholders"]["place"]
    assert str(intent.latitude) in flow["description_placeholders"]["place"]
    assert str(intent.longitude) in flow["description_placeholders"]["place"]
    await jump(h, selected.itinerary.steps[0].departure.strftime("%Y-%m-%d %H:%M"))
    await button.async_press()
    await settle(h)
    after = shown(h).selected_recovery
    with pytest.raises(HomeAssistantError, match="no longer current"):
        await button.async_press()
    duplicate = await configure(h, flow, {"confirmed": True})
    # The same immutable observation ID is idempotent, never the next action.
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "recovery_action_saved"
    assert shown(h).selected_recovery == after


async def test_services_return_bound_prepare_select_confirm_and_stop_responses(hass):
    h = await world(hass)
    plan = shown(h).replacement_plans[0]
    origin = next(step.origin for step in plan.itinerary.steps if step.mode == "BUS")
    offer = await call(
        h,
        "prepare_recovery",
        {"proposal_id": plan.proposal_id, "origin_place_id": origin.physical_id},
    )
    assert offer["id"] and offer["requires_planned_time_acknowledgement"]
    result = await call(
        h,
        "follow_recovery",
        {
            "offer_id": offer["id"],
            "acknowledge_planned_times": True,
            "acknowledge_accessibility_unknown": True,
        },
    )
    assert result["status"] == "active" and result["next_action"] == "boarded"
    selected = shown(h).selected_recovery
    intent = intents(selected)[0]
    await jump(h, selected.itinerary.steps[0].departure.strftime("%Y-%m-%d %H:%M"))
    result = await call(
        h,
        "confirm_recovery_step",
        {
            "selection_id": intent.selection_id,
            "revision": intent.revision,
            "step_index": intent.step_index,
            "kind": intent.kind,
            "place_id": intent.place_id,
            "action_id": "service-observation",
        },
    )
    assert result["revision"] > intent.revision and result["next_action"] == "arrived"
    result = await call(
        h, "stop_recovery", {"selection_id": result["id"], "revision": result["revision"]}
    )
    assert result["status"] == "stopped"


@pytest.mark.parametrize("revision", [True, 0.0, 1.5])
async def test_service_rejects_boolean_or_float_revision_without_state_change(hass, revision):
    h = await world(hass)
    selected = await follow_flow(h)
    with pytest.raises((vol.Invalid, HomeAssistantError)):
        await call(h, "stop_recovery", {"selection_id": selected.id, "revision": revision})
    assert shown(h).selected_recovery == selected


async def test_wrong_physical_place_and_stale_revision_do_not_change_progress(hass):
    h = await world(hass)
    selected = await follow_flow(h)
    intent = intents(selected)[0]
    for changes in ({"place_id": "@place:wrong"}, {"revision": intent.revision + 1}):
        values = {
            "selection_id": intent.selection_id,
            "revision": intent.revision,
            "step_index": intent.step_index,
            "kind": intent.kind,
            "place_id": intent.place_id,
            "action_id": "bad-observation",
            **changes,
        }
        with pytest.raises(HomeAssistantError):
            await call(h, "confirm_recovery_step", values)
        assert shown(h).selected_recovery == selected


async def test_failed_durable_selection_stays_in_offer_form_and_never_claims_success(
    hass, monkeypatch
):
    from train_commute_companion.store import JsonFileStore

    h = await world(hass)
    flow = await prepare_flow(h)
    durable = state_path(hass, h.entry).read_bytes()

    def fail(*args):
        raise StorageError("disk full")

    monkeypatch.setattr(JsonFileStore, "save", fail)
    refused = await configure(
        h, flow, {"confirm_location": True, "planned_times": True, "accessibility_unknown": True}
    )
    await settle(h)
    assert (
        refused["step_id"] == "recovery_offer" and refused["errors"]["base"] == "recovery_refused"
    )
    assert shown(h).selected_recovery is None
    assert state_path(hass, h.entry).read_bytes() == durable

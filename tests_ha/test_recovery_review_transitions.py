from unittest.mock import AsyncMock

from homeassistant.data_entry_flow import FlowResultType
from recovery_sample import selection

from custom_components.train_commute_companion import recovery_flow
from custom_components.train_commute_companion.config_flow import TccOptionsFlow
from custom_components.train_commute_companion.recovery_button import intents
from train_commute_companion.recovery_state import RecoveryOffer
from train_commute_companion.view import RecoveryPlanView


def review_world(hass, monkeypatch):
    selected = selection()
    plan = RecoveryPlanView(
        selected.checked_at,
        selected.itinerary,
        selected.source_query,
        selected.key,
        selected.generation,
        selected.route_revision,
    )
    offer = RecoveryOffer(
        plan.proposal_id,
        selected.key,
        selected.generation,
        selected.route_revision,
        selected.itinerary,
        selected.source_query,
        selected.checked_at,
        selected.selected_at,
    )
    intent = intents(selected)[0]
    flow = TccOptionsFlow()
    flow.hass = hass
    flow._recovery_choices = {plan.proposal_id: plan, intent.id: intent}
    flow._recovery_options = []
    monkeypatch.setattr(flow, "_recovery_data", lambda: object())
    prepare = AsyncMock(return_value=offer)
    follow = AsyncMock(return_value=selected)
    perform = AsyncMock(return_value=selected)
    monkeypatch.setattr(recovery_flow, "prepare", prepare)
    monkeypatch.setattr(recovery_flow, "follow", follow)
    monkeypatch.setattr(recovery_flow, "perform", perform)
    return flow, plan, offer, intent, follow, perform


async def prepare_review(flow, plan):
    result = await flow.async_step_recovery({"choice": plan.proposal_id})
    assert result["step_id"] == "recovery_origin"
    return await flow.async_step_recovery_origin(
        {
            "origin_place_id": plan.itinerary.steps[0].origin.physical_id,
            "confirm_location": True,
        }
    )


async def test_action_review_cannot_submit_a_retained_offer(hass, monkeypatch):
    flow, plan, offer, intent, follow, perform = review_world(hass, monkeypatch)
    await prepare_review(flow, plan)
    result = await flow.async_step_recovery({"choice": intent.id})
    assert result["step_id"] == "recovery_action"
    result = await flow.async_step_recovery_offer(
        {
            "intent_id": offer.id,
            "confirm_location": True,
            "planned_times": True,
            "accessibility_unknown": True,
        }
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "recovery_unavailable"
    follow.assert_not_called()
    perform.assert_not_called()


async def test_offer_review_cannot_submit_a_retained_action(hass, monkeypatch):
    flow, plan, _offer, intent, follow, perform = review_world(hass, monkeypatch)
    await flow.async_step_recovery({"choice": intent.id})
    await prepare_review(flow, plan)
    result = await flow.async_step_recovery_action({"intent_id": intent.id, "confirmed": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "recovery_unavailable"
    follow.assert_not_called()
    perform.assert_not_called()


async def test_offer_confirmation_is_bound_to_presented_offer(hass, monkeypatch):
    flow, plan, _offer, _intent, follow, perform = review_world(hass, monkeypatch)
    await prepare_review(flow, plan)
    result = await flow.async_step_recovery_offer(
        {
            "intent_id": "another-offer",
            "confirm_location": True,
            "planned_times": True,
            "accessibility_unknown": True,
        }
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "recovery_refused"}
    follow.assert_not_called()
    perform.assert_not_called()


async def test_progress_confirmation_is_bound_to_presented_action(hass, monkeypatch):
    flow, _plan, _offer, intent, follow, perform = review_world(hass, monkeypatch)
    await flow.async_step_recovery({"choice": intent.id})
    result = await flow.async_step_recovery_action(
        {"intent_id": "another-action", "confirmed": True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "recovery_refused"}
    follow.assert_not_called()
    perform.assert_not_called()


async def test_offer_review_requires_identity_and_refuses_repeat_commit(hass, monkeypatch):
    flow, plan, offer, _intent, follow, perform = review_world(hass, monkeypatch)
    await prepare_review(flow, plan)
    values = {"confirm_location": True, "planned_times": True, "accessibility_unknown": True}
    missing = await flow.async_step_recovery_offer(values)
    assert missing["errors"] == {"base": "recovery_refused"}
    follow.assert_not_called()
    accepted = await flow.async_step_recovery_offer({**values, "intent_id": offer.id})
    assert accepted["reason"] == "recovery_selected"
    repeated = await flow.async_step_recovery_offer({**values, "intent_id": offer.id})
    assert repeated["reason"] == "recovery_unavailable"
    follow.assert_awaited_once()
    perform.assert_not_called()


async def test_action_review_requires_identity_and_refuses_repeat_commit(hass, monkeypatch):
    flow, _plan, _offer, intent, follow, perform = review_world(hass, monkeypatch)
    await flow.async_step_recovery({"choice": intent.id})
    missing = await flow.async_step_recovery_action({"confirmed": True})
    assert missing["errors"] == {"base": "recovery_refused"}
    perform.assert_not_called()
    values = {"intent_id": intent.id, "confirmed": True}
    accepted = await flow.async_step_recovery_action(values)
    assert accepted["reason"] == "recovery_action_saved"
    repeated = await flow.async_step_recovery_action(values)
    assert repeated["reason"] == "recovery_unavailable"
    perform.assert_awaited_once()
    follow.assert_not_called()


async def test_returning_to_choices_invalidates_previous_review(hass, monkeypatch):
    flow, plan, offer, _intent, follow, perform = review_world(hass, monkeypatch)
    await prepare_review(flow, plan)
    choices = await flow.async_step_recovery()
    assert choices["step_id"] == "recovery"
    result = await flow.async_step_recovery_offer(
        {
            "intent_id": offer.id,
            "confirm_location": True,
            "planned_times": True,
            "accessibility_unknown": True,
        }
    )
    assert result["reason"] == "recovery_unavailable"
    follow.assert_not_called()
    perform.assert_not_called()

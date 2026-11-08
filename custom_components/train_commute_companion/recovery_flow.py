"""Options actions for explicit recovery selection without editing configuration."""

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from train_commute_companion.model import OccurrenceKey
from train_commute_companion.recovery import render
from train_commute_companion.recovery_state import RecoveryOffer
from train_commute_companion.view import RecoveryPlanView

from .data import TccConfigEntry, TccData
from .flow_state import ReviewPhase
from .recovery_button import RecoveryIntent, intents, perform
from .recovery_controls import follow, prepare


class RecoveryReview(StrEnum):
    NONE = "none"
    OFFER = "offer"
    ACTION = "action"


class RecoveryFlow:
    hass: HomeAssistant
    config_entry: TccConfigEntry
    async_show_form: Callable[..., ConfigFlowResult]
    async_abort: Callable[..., ConfigFlowResult]
    _recovery_choices: dict[str, RecoveryPlanView | RecoveryIntent] | None = None
    _recovery_plan: RecoveryPlanView | None = None
    _recovery_offer: RecoveryOffer | None = None
    _recovery_intent: RecoveryIntent | None = None
    _recovery_phase = ReviewPhase.SELECTING
    _recovery_review = RecoveryReview.NONE

    def _recovery_data(self) -> TccData:
        entry = self.config_entry
        data = getattr(entry, "runtime_data", None)
        if not isinstance(data, TccData):
            raise HomeAssistantError("The companion must be running for recovery actions")
        return data

    async def async_step_recovery(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._recovery_phase in (ReviewPhase.COMMITTING, ReviewPhase.COMPLETE):
            return self.async_abort(reason="recovery_unavailable")
        self._recovery_plan = None
        self._recovery_offer = None
        self._recovery_intent = None
        self._recovery_review = RecoveryReview.NONE
        self._recovery_phase = ReviewPhase.SELECTING
        report = ""
        if self._recovery_choices is None:
            try:
                data = self._recovery_data()
                await data.coordinator.async_request_refresh()
            except HomeAssistantError:
                return self.async_abort(reason="recovery_unavailable")
            self._recovery_choices = {}
            options: list[SelectOptionDict] = []
            for occurrence in data.coordinator.data.occurrences:
                if occurrence.not_travelling:
                    continue
                prefix = (
                    f"{occurrence.profile_name}, {occurrence.key.direction.value}, "
                    f"{occurrence.key.service_date}"
                )
                if occurrence.selected_recovery is not None:
                    for intent in intents(occurrence.selected_recovery):
                        self._recovery_choices[intent.id] = intent
                        options.append({"value": intent.id, "label": prefix + ": " + intent.label})
                if occurrence.resolved:
                    continue
                for plan in occurrence.replacement_plans:
                    if not plan.selectable:
                        continue
                    self._recovery_choices[plan.proposal_id] = plan
                    first = plan.itinerary.steps[0]
                    options.append(
                        {
                            "value": plan.proposal_id,
                            "label": prefix + ": prepare " + first.origin.name + " recovery",
                        }
                    )
            self._recovery_options = options
        if not self._recovery_choices:
            return self.async_abort(reason="recovery_unavailable")
        if user_input is not None:
            choice = self._recovery_choices.get(user_input.get("choice", ""))
            if isinstance(choice, RecoveryIntent):
                self._recovery_review = RecoveryReview.ACTION
                self._recovery_phase = ReviewPhase.REVIEWING
                self._recovery_intent = choice
                return await self.async_step_recovery_action()
            if isinstance(choice, RecoveryPlanView):
                self._recovery_phase = ReviewPhase.EDITING
                self._recovery_plan = choice
                return await self.async_step_recovery_origin()
            report = "This recovery choice is no longer available."
        return self.async_show_form(
            step_id="recovery",
            data_schema=vol.Schema(
                {
                    vol.Required("choice"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._recovery_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors={"base": "recovery_refused"} if report else {},
            description_placeholders={"report": report},
        )

    async def async_step_recovery_origin(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._recovery_phase is not ReviewPhase.EDITING or self._recovery_plan is None:
            return self.async_abort(reason="recovery_unavailable")
        plan = self._recovery_plan
        places = {step.origin.physical_id: step.origin for step in plan.itinerary.steps}
        options: list[SelectOptionDict] = [
            {"value": ident, "label": f"{p.name} ({p.latitude:.6f}, {p.longitude:.6f})"}
            for ident, p in places.items()
        ]
        report, errors = "", {}
        if user_input is not None:
            place_id = user_input.get("origin_place_id", "")
            if not user_input.get("confirm_location", False) or place_id not in places:
                errors["base"] = "recovery_acknowledgement"
            else:
                self._recovery_phase = ReviewPhase.COMMITTING
                try:
                    key = cast(OccurrenceKey, plan.occurrence)
                    self._recovery_offer = await prepare(
                        self._recovery_data(), key, plan.proposal_id, place_id
                    )
                except HomeAssistantError as error:
                    self._recovery_phase = ReviewPhase.EDITING
                    errors["base"], report = "recovery_refused", str(error)
                else:
                    self._recovery_review = RecoveryReview.OFFER
                    self._recovery_phase = ReviewPhase.REVIEWING
                    return await self.async_step_recovery_offer()
        return self.async_show_form(
            step_id="recovery_origin",
            data_schema=vol.Schema(
                {
                    vol.Required("origin_place_id"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required("confirm_location", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={"itinerary": render(plan.itinerary), "report": report},
        )

    async def async_step_recovery_offer(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._recovery_phase is not ReviewPhase.REVIEWING
            or self._recovery_review is not RecoveryReview.OFFER
            or self._recovery_offer is None
        ):
            return self.async_abort(reason="recovery_unavailable")
        offer = self._recovery_offer
        report, errors = "", {}
        if user_input is not None:
            if user_input.get("intent_id") != offer.id:
                errors["base"] = "recovery_refused"
                report = "This recovery review is no longer current."
            elif (
                not user_input.get("confirm_location", False)
                or not user_input.get("accessibility_unknown", False)
                or (
                    not offer.itinerary.reported_times
                    and not user_input.get("planned_times", False)
                )
            ):
                errors["base"] = "recovery_acknowledgement"
            else:
                self._recovery_phase = ReviewPhase.COMMITTING
                try:
                    await follow(
                        self._recovery_data(),
                        offer.key,
                        offer.id,
                        planned=bool(user_input.get("planned_times")),
                        accessibility_unknown=bool(user_input.get("accessibility_unknown")),
                    )
                except HomeAssistantError as error:
                    self._recovery_phase = ReviewPhase.REVIEWING
                    errors["base"], report = "recovery_refused", str(error)
                else:
                    self._recovery_phase = ReviewPhase.COMPLETE
                    return self.async_abort(reason="recovery_selected")
        intent_options: list[SelectOptionDict] = [
            {
                "value": offer.id,
                "label": f"{offer.itinerary.steps[0].origin.name} to {offer.itinerary.steps[-1].destination.name}",
            }
        ]
        return self.async_show_form(
            step_id="recovery_offer",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "intent_id", description={"suggested_value": offer.id}
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=intent_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required("confirm_location", default=False): BooleanSelector(),
                    vol.Required("planned_times", default=False): BooleanSelector(),
                    vol.Required("accessibility_unknown", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "itinerary": render(offer.itinerary),
                "origin": offer.itinerary.steps[0].origin.name,
                "checked": offer.checked_at.isoformat(timespec="seconds"),
                "report": report,
            },
        )

    async def async_step_recovery_action(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._recovery_phase is not ReviewPhase.REVIEWING
            or self._recovery_review is not RecoveryReview.ACTION
            or self._recovery_intent is None
        ):
            return self.async_abort(reason="recovery_unavailable")
        intent = self._recovery_intent
        report, errors = "", {}
        if user_input is not None:
            if user_input.get("intent_id") != intent.id:
                errors["base"] = "recovery_refused"
                report = "This recovery review is no longer current."
            elif not user_input.get("confirmed", False):
                errors["base"] = "recovery_acknowledgement"
            else:
                self._recovery_phase = ReviewPhase.COMMITTING
                try:
                    await perform(self._recovery_data(), intent)
                except HomeAssistantError as error:
                    self._recovery_phase = ReviewPhase.REVIEWING
                    errors["base"], report = "recovery_refused", str(error)
                else:
                    self._recovery_phase = ReviewPhase.COMPLETE
                    return self.async_abort(reason="recovery_action_saved")
        intent_options: list[SelectOptionDict] = [{"value": intent.id, "label": intent.label}]
        return self.async_show_form(
            step_id="recovery_action",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "intent_id", description={"suggested_value": intent.id}
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=intent_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required("confirmed", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "action": intent.label,
                "place": intent.place_detail,
                "report": report,
            },
        )

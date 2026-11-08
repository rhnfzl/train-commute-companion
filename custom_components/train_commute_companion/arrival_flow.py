"""Review and record an explicit arrival without changing profile configuration."""

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateTimeSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from train_commute_companion.calendar import TZ
from train_commute_companion.compensation import arrival_text
from train_commute_companion.compensation_state import JourneyRecord
from train_commute_companion.model import okey

from .arrival_controls import confirm_arrival, records
from .data import TccConfigEntry, TccData
from .flow_state import ReviewPhase


def _local_candidates(value: object) -> tuple[datetime, ...]:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return ()
    if not isinstance(parsed, datetime):
        return ()
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        parsed = parsed.astimezone(TZ).replace(tzinfo=None)
    choices: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=TZ, fold=fold)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(TZ).replace(tzinfo=None) == parsed:
            choices.setdefault(instant, candidate)
    return tuple(choices[key] for key in sorted(choices))


def local_timestamp(value: object, offset: object = None) -> datetime:
    """The picker never silently chooses a repeated hour or invents a missing hour."""
    choices = _local_candidates(value)
    if len(choices) == 1:
        return choices[0]
    if len(choices) == 2:
        for choice in choices:
            delta = choice.utcoffset()
            if delta is not None and offset == str(delta.total_seconds()):
                return choice
    raise vol.Invalid("choose an existing Amsterdam time and explicitly resolve repeated hours")


def _human(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%A %d %b %Y at %H:%M %Z (UTC%z)")


def _picker_value(value: object) -> str:
    if isinstance(value, str):
        text = value
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return text
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(TZ)
    # HA's datetime selector splits the date and time on a space.
    return value.strftime("%Y-%m-%d %H:%M:%S")


class ArrivalFlow:
    hass: HomeAssistant
    config_entry: TccConfigEntry
    async_show_form: Callable[..., ConfigFlowResult]
    async_abort: Callable[..., ConfigFlowResult]
    _arrival_choices: dict[str, JourneyRecord] | None = None
    _arrival_record: JourneyRecord | None = None
    _arrival_action_id: str = ""
    _arrival_intent: tuple[str, datetime, datetime | None] | None = None
    _arrival_values: dict[str, Any]
    _arrival_phase = ReviewPhase.SELECTING

    def _arrival_data(self) -> TccData:
        data = getattr(self.config_entry, "runtime_data", None)
        if not isinstance(data, TccData):
            raise HomeAssistantError("The companion must be running to confirm an arrival")
        return data

    async def async_step_arrival(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._arrival_phase in (ReviewPhase.COMMITTING, ReviewPhase.COMPLETE):
            return self.async_abort(reason="arrival_unavailable")
        if self._arrival_choices is None:
            try:
                data = self._arrival_data()
                saved = await records(data)
            except HomeAssistantError:
                return self.async_abort(reason="arrival_unavailable")
            self._arrival_choices = {
                hashlib.sha256(okey(record.key).encode()).hexdigest(): record for record in saved
            }
            self._arrival_names = {
                info.profile_id: info.profile_name for info in data.coordinator.data.directions
            }
        if not self._arrival_choices:
            return self.async_abort(reason="arrival_unavailable")
        errors: dict[str, str] = {}
        if user_input is not None:
            record = self._arrival_choices.get(user_input.get("journey", ""))
            if record is not None:
                self._arrival_record = record
                self._arrival_phase = ReviewPhase.EDITING
                self._arrival_action_id = uuid4().hex
                self._arrival_intent = None
                self._arrival_values = {}
                return await self.async_step_arrival_details()
            errors["base"] = "arrival_refused"
        options: list[SelectOptionDict] = [
            {
                "value": key,
                "label": f"{self._arrival_names.get(r.key.profile_id, r.key.profile_id)}, {r.key.direction.value}, {r.key.service_date}, destination {r.destination}",
            }
            for key, r in self._arrival_choices.items()
        ]
        return self.async_show_form(
            step_id="arrival",
            data_schema=vol.Schema(
                {
                    vol.Required("journey"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_arrival_details(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._arrival_phase is not ReviewPhase.EDITING or self._arrival_record is None:
            return self.async_abort(reason="arrival_unavailable")
        record = self._arrival_record
        values = dict(user_input or self._arrival_values)
        errors: dict[str, str] = {}
        report = ""
        if user_input is not None:
            try:
                arrival = local_timestamp(
                    user_input.get("arrived_at"), user_input.get("arrival_offset")
                )
                original = (
                    local_timestamp(
                        user_input.get("original_planned_arrival"), user_input.get("plan_offset")
                    )
                    if record.plan is None
                    else None
                )
            except vol.Invalid:
                errors["base"] = "arrival_time"
            else:
                self._arrival_values = values
                self._arrival_intent = (user_input.get("destination", ""), arrival, original)
                self._arrival_action_id = uuid4().hex
                self._arrival_phase = ReviewPhase.REVIEWING
                return await self.async_step_arrival_review()
        fields: dict[Any, Any] = {
            vol.Required(
                "destination",
                description={"suggested_value": values.get("destination", record.destination)},
            ): str,
            vol.Required(
                "arrived_at",
                description={
                    "suggested_value": _picker_value(values.get("arrived_at", record.arrived_at))
                },
            ): DateTimeSelector(),
        }
        if record.plan is None:
            fields[
                vol.Required(
                    "original_planned_arrival",
                    description={
                        "suggested_value": _picker_value(values.get("original_planned_arrival"))
                    },
                )
            ] = DateTimeSelector()
        for timestamp, field in (
            ("arrived_at", "arrival_offset"),
            ("original_planned_arrival", "plan_offset"),
        ):
            choices = _local_candidates(
                values.get(timestamp, record.arrived_at if timestamp == "arrived_at" else None)
            )
            if len(choices) == 2 and (timestamp == "arrived_at" or record.plan is None):
                offset_options: list[SelectOptionDict] = []
                for choice in choices:
                    delta = choice.utcoffset()
                    assert delta is not None
                    offset_options.append(
                        {"value": str(delta.total_seconds()), "label": _human(choice)}
                    )
                fields[vol.Required(field)] = SelectSelector(
                    SelectSelectorConfig(
                        options=offset_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
        reference = (
            f"Saved planned arrival: {_human(record.plan.planned_arrival)}; source {record.plan.source}."
            if record.plan is not None
            else "No original plan was saved. Enter the planned arrival you intended, using the timetable for that travel date."
        )
        previous = _human(record.arrived_at) if record.arrived_at is not None else "not confirmed"
        return self.async_show_form(
            step_id="arrival_details",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "journey": f"{self._arrival_names.get(record.key.profile_id, record.key.profile_id)}, {record.key.direction.value}, {record.key.service_date:%A %d %b %Y}",
                "destination": record.destination,
                "reference": reference,
                "previous": previous,
                "report": report,
            },
        )

    async def async_step_arrival_review(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._arrival_phase is not ReviewPhase.REVIEWING
            or self._arrival_record is None
            or self._arrival_intent is None
        ):
            return self.async_abort(reason="arrival_unavailable")
        record = self._arrival_record
        destination, arrival, original = self._arrival_intent
        errors: dict[str, str] = {}
        report = ""
        if user_input is not None:
            if user_input.get("intent_id") != self._arrival_action_id:
                errors["base"] = "arrival_refused"
            elif user_input.get("edit", False):
                self._arrival_phase = ReviewPhase.EDITING
                self._arrival_intent = None
                self._arrival_action_id = uuid4().hex
                return await self.async_step_arrival_details()
            elif not user_input.get("confirmed", False):
                errors["base"] = "arrival_acknowledgement"
            else:
                self._arrival_phase = ReviewPhase.COMMITTING
                try:
                    result = await confirm_arrival(
                        self._arrival_data(),
                        record.key,
                        destination,
                        arrival,
                        expected_revision=record.revision,
                        action_id=self._arrival_action_id,
                        original_planned_arrival=original,
                    )
                except HomeAssistantError as error:
                    self._arrival_phase = ReviewPhase.REVIEWING
                    errors["base"], report = "arrival_refused", str(error)
                else:
                    self._arrival_phase = ReviewPhase.COMPLETE
                    return self.async_abort(
                        reason="arrival_saved",
                        description_placeholders={"result": arrival_text(result)},
                    )
        planned = record.plan.planned_arrival if record.plan is not None else original
        assert planned is not None
        intent_options: list[SelectOptionDict] = [
            {
                "value": self._arrival_action_id,
                "label": f"{destination}: {_human(arrival)}",
            }
        ]
        return self.async_show_form(
            step_id="arrival_review",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "intent_id", description={"suggested_value": self._arrival_action_id}
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=intent_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                    vol.Required("confirmed", default=False): BooleanSelector(),
                    vol.Optional("edit", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "journey": f"{self._arrival_names.get(record.key.profile_id, record.key.profile_id)}, {record.key.direction.value}, {record.key.service_date:%A %d %b %Y}",
                "destination": destination,
                "arrival": _human(arrival),
                "planned": _human(planned),
                "report": report,
            },
        )

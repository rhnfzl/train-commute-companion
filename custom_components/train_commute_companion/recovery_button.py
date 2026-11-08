"""Recovery buttons bind one physical confirmation to one immutable rendered step."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from train_commute_companion.model import OccurrenceKey
from train_commute_companion.recovery_state import SelectedRecovery
from train_commute_companion.view import RuntimeView

from .coordinator import TccCoordinator
from .data import TccData
from .entity import TccEntity
from .recovery_controls import confirm, stop


@dataclass(frozen=True, slots=True)
class RecoveryIntent:
    key: OccurrenceKey
    selection_id: str
    revision: int
    step_index: int
    kind: str
    place_id: str
    label: str
    place_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    @property
    def place_detail(self) -> str:
        if self.kind == "stop":
            return "Stopping asserts no physical movement."
        return (
            f"Physical place: {self.place_name}; latitude {self.latitude}; "
            f"longitude {self.longitude}; place ID {self.place_id}."
        )

    @property
    def id(self) -> str:
        fields = (
            self.key.profile_id,
            self.key.direction.value,
            self.key.service_date.isoformat(),
            self.selection_id,
            str(self.revision),
            str(self.step_index),
            self.kind,
            self.place_id,
        )
        return hashlib.sha256("|".join(fields).encode()).hexdigest()[:24]


def intents(selected: SelectedRecovery) -> tuple[RecoveryIntent, ...]:
    if selected.status != "active" or selected.next_action is None:
        return ()
    step = selected.itinerary.steps[selected.index]
    kind = selected.next_action
    place = step.origin if kind == "boarded" else step.destination
    label = (
        f"I boarded {step.operator or ''} {step.mode} {step.service_reference or ''} at {place.name}"
        if kind == "boarded"
        else f"I reached {place.name}"
    )
    args = (selected.key, selected.id, selected.revision, selected.index)
    return (
        RecoveryIntent(
            *args, kind, place.physical_id, label, place.name, place.latitude, place.longitude
        ),
        RecoveryIntent(*args, "stop", "", "Stop following this recovery"),
    )


async def perform(data: TccData, intent: RecoveryIntent) -> SelectedRecovery:
    if intent.kind == "stop":
        return await stop(data, intent.key, intent.selection_id, intent.revision)
    return await confirm(
        data,
        intent.key,
        intent.selection_id,
        intent.revision,
        intent.step_index,
        intent.kind,
        intent.place_id,
        "ha-" + intent.id,
    )


class RecoveryButton(TccEntity, ButtonEntity):
    _attr_icon = "mdi:map-marker-check"

    def __init__(
        self, coordinator: TccCoordinator, data: TccData, name: str, intent: RecoveryIntent
    ) -> None:
        super().__init__(coordinator, intent.key.profile_id, name, "recovery", intent.key.direction)
        self._data, self.intent = data, intent
        self._attr_unique_id = "recovery_" + intent.id
        self._attr_translation_key = None
        self._attr_name = intent.label

    @property
    def suggested_object_id(self) -> str:
        # A stale entity ID cannot become a new step's action after entity replacement.
        return "tcc_recovery_" + self.intent.id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        intent = self.intent
        return {
            "selection_id": intent.selection_id,
            "revision": intent.revision,
            "step_index": intent.step_index,
            "kind": intent.kind,
            "place_id": intent.place_id or None,
            "place_name": intent.place_name,
            "latitude": intent.latitude,
            "longitude": intent.longitude,
            "confirmation_source": "explicit user action",
        }

    @property
    def available(self) -> bool:
        return super().available and any(
            self.intent in intents(occ.selected_recovery)
            for occ in self.coordinator.data.occurrences
            if occ.key == self.intent.key
            and not occ.not_travelling
            and occ.selected_recovery is not None
        )

    async def async_press(self) -> None:
        if not self.available:
            raise HomeAssistantError("This recovery action is no longer current")
        await perform(self._data, self.intent)


def wanted_recovery_buttons(
    coordinator: TccCoordinator, data: TccData, view: RuntimeView
) -> dict[str, Callable[[], TccEntity]]:
    out: dict[str, Callable[[], TccEntity]] = {}
    for occurrence in view.occurrences:
        if occurrence.not_travelling or occurrence.selected_recovery is None:
            continue
        for intent in intents(occurrence.selected_recovery):

            def make(i: RecoveryIntent = intent, name: str = occurrence.profile_name) -> TccEntity:
                return RecoveryButton(coordinator, data, name, i)

            out["recovery_" + intent.id] = make
    return out

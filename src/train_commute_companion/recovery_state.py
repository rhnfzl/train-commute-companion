"""Stored recovery selections and explicit observations, separate from train progress."""

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from train_commute_companion.model import EventKind, OccurrenceKey
from train_commute_companion.providers.base import Query
from train_commute_companion.recovery import Place, RecoveryItinerary, payload
from train_commute_companion.serde import encode

ORIGINAL_ROUTINE = frozenset(
    {
        EventKind.LEAVE_NOW,
        EventKind.NEXT_CONNECTION,
        EventKind.EARLIER_TRAIN_WITHDRAWN,
        EventKind.ALL_GOOD,
    }
)

RecoveryActionKind = Literal["boarded", "arrived", "walk_arrived", "stop"]


class RecoveryOfferPhase(StrEnum):
    CURRENT = "current"
    STALE_CONTEXT = "stale_context"
    STALE_SOURCE = "stale_source"


class RecoveryProgress(StrEnum):
    WAITING_TO_BOARD = "waiting_to_board"
    ABOARD = "aboard"
    WALKING = "walking"
    COMPLETED = "completed"
    STOPPED = "stopped"
    INVALID = "invalid"


class RecoverySourcePhase(StrEnum):
    CURRENT = "current"
    UNAVAILABLE = "unavailable"
    CHANGED = "changed"


def _fingerprint_value(value: object) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("offer timestamp has no timezone")
        return value.astimezone(UTC).isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _fingerprint_value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, tuple | list):
        return [_fingerprint_value(item) for item in value]
    return encode(value)


@dataclass(frozen=True, slots=True)
class RecoveryOffer:
    proposal_id: str
    key: OccurrenceKey
    generation: int
    route_revision: int
    itinerary: RecoveryItinerary
    source_query: Query
    checked_at: datetime
    offered_at: datetime

    def phase_at(self, now: datetime, generation: int, route_revision: int) -> RecoveryOfferPhase:
        from train_commute_companion.health import FRESH_FOR

        if (generation, route_revision) != (self.generation, self.route_revision):
            return RecoveryOfferPhase.STALE_CONTEXT
        if any(t.tzinfo is None or t.utcoffset() is None for t in (now, self.checked_at)):
            return RecoveryOfferPhase.STALE_SOURCE
        age = now.astimezone(UTC) - self.checked_at.astimezone(UTC)
        if age.total_seconds() < 0 or age >= FRESH_FOR:
            return RecoveryOfferPhase.STALE_SOURCE
        return RecoveryOfferPhase.CURRENT

    @property
    def id(self) -> str:
        return self.fingerprint[:24]

    def fields(self) -> dict[str, object]:
        return {
            "id": self.id,
            "proposal_id": self.proposal_id,
            "checked_at": self.checked_at.isoformat(),
            "itinerary": payload(self.itinerary),
            "requires_planned_time_acknowledgement": not self.itinerary.reported_times,
            "requires_accessibility_acknowledgement": True,
        }

    @property
    def fingerprint(self) -> str:
        """Bind acknowledgements to all displayed timings, places and source evidence."""
        canonical = json.dumps(_fingerprint_value(self), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    id: str
    selection_id: str
    revision: int
    step_index: int
    kind: RecoveryActionKind
    place: Place | None
    at: datetime


@dataclass(frozen=True, slots=True)
class PhysicalConfirmation:
    place: Place
    at: datetime


@dataclass(frozen=True, slots=True)
class SelectedRecovery:
    id: str
    key: OccurrenceKey
    generation: int
    route_revision: int
    itinerary: RecoveryItinerary
    source_query: Query
    checked_at: datetime
    selected_at: datetime
    origin: PhysicalConfirmation
    approved_fingerprint: str
    planned_times_acknowledged: bool
    accessibility_unknown_acknowledged: bool
    revision: int = 0
    index: int = 0
    confirmations: tuple[RecoveryAction, ...] = ()
    status: Literal["active", "completed", "stopped"] = "active"
    ended_at: datetime | None = None
    monitoring_error: str | None = None
    last_attempt: datetime | None = None
    review_required: bool = False
    monitoring_issue: Literal["current", "unavailable", "changed"] = "current"
    monitoring_revision: int = 0

    @property
    def progress_phase(self) -> RecoveryProgress:
        if self.status == "completed":
            return RecoveryProgress.COMPLETED
        if self.status == "stopped":
            return RecoveryProgress.STOPPED
        if self.status != "active" or not 0 <= self.index < len(self.itinerary.steps):
            return RecoveryProgress.INVALID
        if self.itinerary.steps[self.index].mode == "WALK":
            return RecoveryProgress.WALKING
        if any(c.kind == "boarded" and c.step_index == self.index for c in self.confirmations):
            return RecoveryProgress.ABOARD
        return RecoveryProgress.WAITING_TO_BOARD

    @property
    def active(self) -> bool:
        return self.progress_phase in (
            RecoveryProgress.WAITING_TO_BOARD,
            RecoveryProgress.ABOARD,
            RecoveryProgress.WALKING,
        )

    @property
    def source_phase(self) -> RecoverySourcePhase:
        if self.review_required and self.monitoring_issue == "current":
            return RecoverySourcePhase.CHANGED
        return RecoverySourcePhase(self.monitoring_issue)

    def update_monitoring(
        self,
        phase: RecoverySourcePhase,
        *,
        error: str | None,
        checked_at: datetime,
        attempted_at: datetime,
    ) -> "SelectedRecovery":
        if self.review_required and phase is RecoverySourcePhase.CURRENT:
            phase = RecoverySourcePhase.CHANGED
            error = (
                self.monitoring_error or "Review a new recovery plan before relying on its times."
            )
        return replace(
            self,
            monitoring_error=error,
            checked_at=checked_at,
            last_attempt=attempted_at,
            review_required=self.review_required or phase is RecoverySourcePhase.CHANGED,
            monitoring_issue=phase.value,
            monitoring_revision=self.monitoring_revision + (phase != self.monitoring_issue),
        )

    @property
    def next_action(self) -> RecoveryActionKind | None:
        actions: dict[RecoveryProgress, RecoveryActionKind] = {
            RecoveryProgress.WAITING_TO_BOARD: "boarded",
            RecoveryProgress.ABOARD: "arrived",
            RecoveryProgress.WALKING: "walk_arrived",
        }
        return actions.get(self.progress_phase)

    def fields(self, now: datetime) -> dict[str, object]:
        from train_commute_companion.health import FRESH_FOR

        age = now.astimezone(UTC) - self.checked_at.astimezone(UTC)
        itinerary = payload(self.itinerary)
        itinerary.update(followed=True, origin_confirmed=True)
        return {
            "id": self.id,
            "revision": self.revision,
            "index": self.index,
            "status": self.status,
            "next_action": self.next_action,
            "itinerary": itinerary,
            "confirmations": encode(self.confirmations),
            "checked_at": self.checked_at.isoformat(),
            "monitoring_error": self.monitoring_error,
            "review_required": self.review_required,
            "monitoring_issue": self.monitoring_issue,
            "source_fresh": age.total_seconds() >= 0
            and age < FRESH_FOR
            and self.monitoring_error is None
            and self.source_phase is RecoverySourcePhase.CURRENT,
            "progress_source": "explicit user confirmations",
        }

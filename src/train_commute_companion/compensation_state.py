"""Saved plans and explicit arrivals survive the active journey's monitoring window."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Literal

from train_commute_companion.calendar import to_utc
from train_commute_companion.model import OccurrenceKey


class PlanPhase(StrEnum):
    MISSING = "missing"
    CAPTURED = "captured"


class ArrivalRecordPhase(StrEnum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"


class ReminderPhase(StrEnum):
    UNCREATED = "uncreated"
    QUEUED = "queued"


@dataclass(frozen=True, slots=True)
class PlannedJourney:
    destination: str
    planned_departure: datetime | None
    planned_arrival: datetime
    source: Literal["ns", "user"]
    checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ArrivalConfirmation:
    action_id: str
    destination: str
    at: datetime
    original_planned_arrival: datetime | None = None


@dataclass(frozen=True, slots=True)
class JourneyRecord:
    key: OccurrenceKey
    destination: str
    plan: PlannedJourney | None
    route_revision: int
    revision: int = 0
    confirmations: tuple[ArrivalConfirmation, ...] = ()
    reminder_created: bool = False

    @property
    def plan_phase(self) -> PlanPhase:
        return PlanPhase.MISSING if self.plan is None else PlanPhase.CAPTURED

    @property
    def arrival_phase(self) -> ArrivalRecordPhase:
        count = len(self.confirmations)
        if count == 0:
            return ArrivalRecordPhase.UNCONFIRMED
        return ArrivalRecordPhase.CONFIRMED if count == 1 else ArrivalRecordPhase.CORRECTED

    @property
    def reminder_phase(self) -> ReminderPhase:
        return ReminderPhase.QUEUED if self.reminder_created else ReminderPhase.UNCREATED

    def reminder_due(self, enabled: bool) -> bool:
        delay = self.delay_minutes
        return (
            enabled
            and self.reminder_phase is ReminderPhase.UNCREATED
            and self.arrival_phase is not ArrivalRecordPhase.UNCONFIRMED
            and delay is not None
            and delay >= 30
        )

    def queue_reminder(self) -> "JourneyRecord":
        if (
            self.arrival_phase is ArrivalRecordPhase.UNCONFIRMED
            or self.plan_phase is PlanPhase.MISSING
        ):
            raise ValueError("arrival and original plan are required before a reminder")
        return replace(self, reminder_created=True)

    @property
    def arrived_at(self) -> datetime | None:
        return (
            self.confirmations[-1].at
            if self.arrival_phase is not ArrivalRecordPhase.UNCONFIRMED
            else None
        )

    @property
    def arrival_source(self) -> Literal["user"] | None:
        return "user" if self.arrival_phase is not ArrivalRecordPhase.UNCONFIRMED else None

    @property
    def delay_minutes(self) -> int | None:
        arrived = self.arrived_at
        if self.plan is None or arrived is None:
            return None
        return int((to_utc(arrived) - to_utc(self.plan.planned_arrival)).total_seconds() / 60)

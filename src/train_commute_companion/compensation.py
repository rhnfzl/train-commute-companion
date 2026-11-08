"""Compare a user's confirmed arrival with a saved plan, without deciding claim eligibility."""

from dataclasses import replace
from datetime import datetime, time

from train_commute_companion.calendar import resolve_local, to_utc
from train_commute_companion.compensation_state import (
    ArrivalConfirmation,
    JourneyRecord,
    PlannedJourney,
    PlanPhase,
)
from train_commute_companion.model import Connection, present

NS_COMPENSATION = "https://www.ns.nl/service-en-contact/geld-terug/geld-terug-bij-vertraging"


class ArrivalError(ValueError):
    pass


def _aware(value: datetime) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def reference_from_connection(
    connection: Connection, checked_at: datetime | None = None
) -> PlannedJourney | None:
    if not connection.legs:
        return None
    first, last = connection.legs[0], connection.legs[-1]
    dep, arr = first.board.planned_departure, last.alight.planned_arrival
    if (
        not last.alight.code
        or dep is None
        or arr is None
        or not _aware(dep)
        or not _aware(arr)
        or to_utc(arr) < to_utc(dep)
        or (checked_at is not None and not _aware(checked_at))
    ):
        return None
    return PlannedJourney(last.alight.code, dep, arr, "ns", checked_at)


def confirm(
    record: JourneyRecord,
    destination: str,
    arrived_at: datetime,
    *,
    now: datetime,
    expected_revision: int,
    action_id: str,
    original_planned_arrival: datetime | None = None,
) -> JourneyRecord:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ArrivalError("Arrival revision must be a non-negative whole number.")
    if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
        raise ArrivalError("An arrival action identifier is required.")
    if destination != record.destination:
        raise ArrivalError("Confirm the saved journey's exact destination station.")
    if not _aware(arrived_at) or not _aware(now):
        raise ArrivalError("Use a full arrival date and time with a timezone offset.")
    if original_planned_arrival is not None and not _aware(original_planned_arrival):
        raise ArrivalError("Use an original planned arrival with a timezone offset.")
    action = ArrivalConfirmation(action_id, destination, arrived_at, original_planned_arrival)
    previous = next((a for a in record.confirmations if a.action_id == action_id), None)
    if previous is not None:
        if (
            previous.destination == destination
            and to_utc(previous.at) == to_utc(arrived_at)
            and (
                previous.original_planned_arrival is None
                and original_planned_arrival is None
                or previous.original_planned_arrival is not None
                and original_planned_arrival is not None
                and to_utc(previous.original_planned_arrival) == to_utc(original_planned_arrival)
            )
        ):
            return record
        raise ArrivalError("That arrival action identifier already records different details.")
    if expected_revision != record.revision:
        raise ArrivalError("The saved arrival changed. Review its current details first.")
    if to_utc(arrived_at) > to_utc(now):
        raise ArrivalError("The arrival time cannot be in the future.")
    plan = record.plan
    if record.plan_phase is PlanPhase.MISSING:
        if original_planned_arrival is None:
            raise ArrivalError(
                "No original plan was saved. Enter your original planned arrival explicitly."
            )
        plan = PlannedJourney(destination, None, original_planned_arrival, "user")
    elif original_planned_arrival is not None and to_utc(original_planned_arrival) != to_utc(
        present(plan).planned_arrival
    ):
        raise ArrivalError("The saved original plan is immutable.")
    plan = present(plan)
    service_start = to_utc(resolve_local(record.key.service_date, time.min, edge="start"))
    if to_utc(arrived_at) < service_start:
        raise ArrivalError("The arrival precedes the journey's service date.")
    if to_utc(plan.planned_arrival) < service_start:
        raise ArrivalError("The original planned arrival precedes the journey's service date.")
    if plan.planned_departure is not None and to_utc(arrived_at) < to_utc(plan.planned_departure):
        raise ArrivalError("The arrival time precedes the saved departure.")
    return replace(
        record,
        plan=plan,
        revision=record.revision + 1,
        confirmations=(*record.confirmations, action),
    )


def arrival_text(record: JourneyRecord) -> str:
    minutes = record.delay_minutes
    comparison = (
        f"{minutes} minutes after"
        if minutes is not None and minutes >= 0
        else f"{abs(minutes or 0)} minutes before"
    )
    return (
        f"Your confirmed arrival at {record.destination} is {comparison} your saved plan. "
        "Check current NS compensation rules. Adjusted timetables and other conditions may affect your claim. "
        "This comparison does not decide eligibility. " + NS_COMPENSATION
    )

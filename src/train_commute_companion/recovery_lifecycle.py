"""Validate recovery selections and record manual progress. Time alone changes no state."""

import math
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from train_commute_companion.location import STALE_LOCATION
from train_commute_companion.model import Provenance
from train_commute_companion.recovery import Place, RecoveryItinerary, validate_catchability
from train_commute_companion.recovery_state import (
    PhysicalConfirmation,
    RecoveryAction,
    RecoveryOffer,
    RecoveryOfferPhase,
    SelectedRecovery,
)


class RecoveryActionError(ValueError):
    """A recovery action cannot safely apply to the current selection."""


def _utc(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise RecoveryActionError("confirmation time has no timezone")
    return at.astimezone(UTC)


def _same(a: Place, b: Place) -> bool:
    def valid(place: Place) -> bool:
        return (
            bool(place.physical_id.strip())
            and bool(place.name.strip())
            and isinstance(place.latitude, int | float)
            and isinstance(place.longitude, int | float)
            and not isinstance(place.latitude, bool)
            and not isinstance(place.longitude, bool)
            and math.isfinite(place.latitude)
            and math.isfinite(place.longitude)
            and -90 <= place.latitude <= 90
            and -180 <= place.longitude <= 180
        )

    return (
        valid(a)
        and valid(b)
        and (
            a.physical_id == b.physical_id
            and (a.latitude, a.longitude) == (b.latitude, b.longitude)
        )
    )


def _itinerary(itinerary: RecoveryItinerary) -> None:
    if not itinerary.steps:
        raise RecoveryActionError("itinerary has no steps")
    previous = None
    for step in itinerary.steps:
        if not _same(step.origin, step.origin) or not _same(step.destination, step.destination):
            raise RecoveryActionError("itinerary physical endpoints are unknown")
        if step.mode not in ("TRAIN", "BUS", "METRO", "TRAM", "FERRY", "WALK"):
            raise RecoveryActionError("itinerary mode is unknown")
        if step.departure_provenance not in (
            Provenance.REPORTED,
            Provenance.PLANNED,
        ) or step.arrival_provenance not in (Provenance.REPORTED, Provenance.PLANNED):
            raise RecoveryActionError("itinerary endpoint time is missing")
        if step.mode != "WALK" and step.service_key is None:
            raise RecoveryActionError("vehicle has no stable service identity")
        if step.planned_departure is None or step.planned_arrival is None:
            raise RecoveryActionError("itinerary planned times are unknown")
        if _utc(step.arrival) < _utc(step.departure) or _utc(step.planned_arrival) < _utc(
            step.planned_departure
        ):
            raise RecoveryActionError("itinerary time is reversed")
        if previous is not None and (
            not _same(previous.destination, step.origin)
            or _utc(step.departure) < _utc(previous.arrival)
        ):
            raise RecoveryActionError("itinerary is disconnected")
        previous = step
    vehicles = [i for i, step in enumerate(itinerary.steps) if step.mode != "WALK"]
    expected = tuple(pairwise(vehicles))
    if tuple((t.incoming_step, t.outgoing_step) for t in itinerary.transfers) != expected:
        raise RecoveryActionError("vehicle transfer records are incomplete")
    for transfer in itinerary.transfers:
        incoming, outgoing = transfer.incoming_step, transfer.outgoing_step
        walks = tuple(i for i in range(incoming + 1, outgoing) if itinerary.steps[i].mode == "WALK")
        walking = sum(
            (_utc(itinerary.steps[i].arrival) - _utc(itinerary.steps[i].departure) for i in walks),
            timedelta(),
        )
        available = _utc(itinerary.steps[outgoing].departure) - _utc(
            itinerary.steps[incoming].arrival
        )
        if (
            transfer.walk_steps != walks
            or transfer.walking != walking
            or transfer.waiting != available - walking
            or transfer.minimum < timedelta()
            or available < transfer.minimum
        ):
            raise RecoveryActionError("transfer timing is inconsistent or below the minimum")


def select_recovery(
    offer: RecoveryOffer,
    *,
    selection_id: str,
    fingerprint: str,
    confirmed_origin: Place,
    confirmed_at: datetime,
    now: datetime,
    generation: int,
    route_revision: int,
    require_accessible: bool,
    acknowledge_planned_times: bool = False,
    acknowledge_accessibility_unknown: bool = False,
) -> SelectedRecovery:
    """Only select the fresh offer the user reviewed. This performs no write or network call."""
    if any(
        type(value) is not bool
        for value in (
            require_accessible,
            offer.source_query.require_accessible,
            acknowledge_planned_times,
            acknowledge_accessibility_unknown,
        )
    ):
        raise RecoveryActionError(
            "selection acknowledgements and accessibility preference must be booleans"
        )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (
            generation,
            route_revision,
            offer.generation,
            offer.route_revision,
        )
    ):
        raise RecoveryActionError("occurrence revisions must be nonnegative whole numbers")
    if not selection_id.strip() or not offer.proposal_id.strip():
        raise RecoveryActionError("selection identity is missing")
    try:
        current_fingerprint = offer.fingerprint
    except (ValueError, OverflowError):
        raise RecoveryActionError("offer timestamp is invalid") from None
    if fingerprint != current_fingerprint:
        raise RecoveryActionError("the reviewed offer changed")
    if offer.generation != generation or offer.route_revision != route_revision:
        raise RecoveryActionError("the occurrence changed")
    query = offer.source_query
    for station in (query.origin, query.destination, *((query.via,) if query.via else ())):
        if station == "8400002" or re.fullmatch(r"[A-Za-z0-9]+", station) is None:
            raise RecoveryActionError("source query has no supported station anchor")
    _utc(query.at)
    if query.require_accessible != require_accessible:
        raise RecoveryActionError("accessibility request changed")
    _utc(now)
    _utc(offer.checked_at)
    if offer.phase_at(now, generation, route_revision) is not RecoveryOfferPhase.CURRENT:
        raise RecoveryActionError("provider source is not current")
    if not _utc(offer.checked_at) <= _utc(offer.offered_at) <= _utc(now):
        raise RecoveryActionError("offer time is invalid")
    if not timedelta() <= _utc(now) - _utc(confirmed_at) <= STALE_LOCATION:
        raise RecoveryActionError("physical origin confirmation is not current")
    _itinerary(offer.itinerary)
    if not _same(confirmed_origin, offer.itinerary.steps[0].origin):
        raise RecoveryActionError("confirmed origin is a different physical place")
    check = validate_catchability(
        offer.itinerary, confirmed_origin, confirmed_at, now, checked_at=offer.checked_at
    )
    if check.state not in ("feasible", "unconfirmed"):
        raise RecoveryActionError(check.reason)
    if not offer.itinerary.reported_times and acknowledge_planned_times is not True:
        raise RecoveryActionError("planned times need acknowledgement")
    if acknowledge_accessibility_unknown is not True:
        raise RecoveryActionError("unknown accessibility needs acknowledgement")
    return SelectedRecovery(
        selection_id,
        offer.key,
        generation,
        route_revision,
        offer.itinerary,
        query,
        offer.checked_at,
        now,
        PhysicalConfirmation(confirmed_origin, confirmed_at),
        fingerprint,
        acknowledge_planned_times,
        acknowledge_accessibility_unknown,
    )


def _duplicate(a: RecoveryAction, b: RecoveryAction) -> bool:
    return (
        a.id == b.id
        and a.selection_id == b.selection_id
        and a.revision == b.revision
        and a.step_index == b.step_index
        and a.kind == b.kind
        and (
            (a.place is None and b.place is None)
            or (a.place is not None and b.place is not None and _same(a.place, b.place))
        )
        and _utc(a.at) == _utc(b.at)
    )


def apply_manual(
    selection: SelectedRecovery, action: RecoveryAction, *, now: datetime
) -> SelectedRecovery:
    """Provider availability never changes an explicit observation or advances a step."""
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (action.revision, action.step_index, selection.revision, selection.index)
    ):
        raise RecoveryActionError(
            "recovery revision and step index must be nonnegative whole numbers"
        )
    if not action.id.strip() or action.selection_id != selection.id:
        raise RecoveryActionError("selection identity changed")
    previous = next((old for old in selection.confirmations if old.id == action.id), None)
    if previous is not None:
        if _duplicate(previous, action):
            return selection
        raise RecoveryActionError("action identity was reused")
    if selection.status != "active":
        raise RecoveryActionError("selection is no longer active")
    if action.revision != selection.revision or action.step_index != selection.index:
        raise RecoveryActionError("recovery step changed")
    last_at = selection.confirmations[-1].at if selection.confirmations else selection.selected_at
    if not _utc(last_at) <= _utc(action.at) <= _utc(now):
        raise RecoveryActionError("confirmation time is reversed or in the future")
    if action.kind == "stop":
        return replace(
            selection,
            revision=selection.revision + 1,
            status="stopped",
            ended_at=action.at,
            confirmations=(*selection.confirmations, action),
        )
    if not 0 <= selection.index < len(selection.itinerary.steps):
        raise RecoveryActionError("recovery step is missing")
    step = selection.itinerary.steps[selection.index]
    if action.place is None:
        raise RecoveryActionError("physical place confirmation is missing")
    expected = selection.next_action
    if action.kind == "boarded":
        if expected != "boarded" or not _same(action.place, step.origin):
            raise RecoveryActionError("boarding confirmation does not match the current vehicle")
        index = selection.index
    elif action.kind == "arrived":
        if expected != "arrived" or not _same(action.place, step.destination):
            raise RecoveryActionError("arrival confirmation does not match the boarded vehicle")
        index = selection.index + 1
    elif action.kind == "walk_arrived":
        if expected != "walk_arrived" or not _same(action.place, step.destination):
            raise RecoveryActionError("walking destination confirmation does not match")
        index = selection.index + 1
    else:
        raise RecoveryActionError("unknown recovery action")
    completed = index == len(selection.itinerary.steps)
    return replace(
        selection,
        revision=selection.revision + 1,
        index=index,
        confirmations=(*selection.confirmations, action),
        status="completed" if completed else "active",
        ended_at=action.at if completed else None,
    )

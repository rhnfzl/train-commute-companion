from dataclasses import replace
from datetime import timedelta

import pytest
from recovery_sample import NOW, answer

from train_commute_companion.model import Direction, OccurrenceKey
from train_commute_companion.recovery import from_connection
from train_commute_companion.recovery_lifecycle import (
    RecoveryActionError,
    apply_manual,
    select_recovery,
)
from train_commute_companion.recovery_state import RecoveryAction, RecoveryOffer, SelectedRecovery
from train_commute_companion.serde import dumps, loads


def offer():
    source = answer()
    connection = replace(source.connections[0], legs=source.connections[0].legs[2:])
    itinerary = from_connection(connection)
    return RecoveryOffer(
        "proposal",
        OccurrenceKey("p1", Direction.OUTBOUND, NOW.date()),
        3,
        2,
        itinerary,
        source.query,
        NOW,
        NOW,
    )


def select(candidate=None, **changes):
    candidate = candidate or offer()
    options = {
        "selection_id": "selected",
        "fingerprint": candidate.fingerprint,
        "confirmed_origin": candidate.itinerary.steps[0].origin,
        "confirmed_at": NOW,
        "now": NOW,
        "generation": 3,
        "route_revision": 2,
        "require_accessible": False,
        "acknowledge_planned_times": True,
        "acknowledge_accessibility_unknown": True,
    }
    options.update(changes)
    return select_recovery(candidate, **options)


def action(selection, kind, *, place=None, at=None, action_id=None):
    step = selection.itinerary.steps[selection.index]
    place = place or (step.origin if kind == "boarded" else step.destination)
    at = at or step.departure
    return RecoveryAction(
        action_id or f"{kind}-{selection.index}",
        selection.id,
        selection.revision,
        selection.index,
        kind,
        place,
        at,
    )


def test_selected_plan_roundtrips_with_query_steps_and_transfer_durations():
    candidate = offer()
    selected = select(candidate)
    assert selected.status == "active" and selected.index == 0
    assert selected.source_query == candidate.source_query
    assert selected.itinerary.transfers[0].walking == timedelta(minutes=5)
    assert loads(SelectedRecovery, dumps(selected)) == selected
    assert loads(RecoveryOffer, dumps(candidate)).fingerprint == candidate.fingerprint


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 4},
        {"route_revision": 4},
        {"fingerprint": "old"},
        {"require_accessible": True},
        {"acknowledge_planned_times": False},
        {"acknowledge_accessibility_unknown": False},
    ],
)
def test_selection_refuses_stale_context_and_missing_acknowledgements(changes):
    with pytest.raises(RecoveryActionError):
        select(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"checked_at": NOW - timedelta(minutes=5)},
        {"checked_at": NOW + timedelta(seconds=1)},
        {"source_query": replace(answer().query, origin="@place:unsafe")},
    ],
)
def test_selection_refuses_old_source_and_nonstation_queries(changes):
    with pytest.raises(RecoveryActionError):
        select(replace(offer(), **changes))


def test_origin_identity_and_stable_vehicle_identity_are_required():
    candidate = offer()
    origin = candidate.itinerary.steps[0].origin
    with pytest.raises(RecoveryActionError):
        select(candidate, confirmed_origin=replace(origin, physical_id="rail-station"))
    steps = candidate.itinerary.steps
    missing = replace(steps[0], service_reference=None)
    with pytest.raises(RecoveryActionError):
        select(
            replace(candidate, itinerary=replace(candidate.itinerary, steps=(missing, *steps[1:])))
        )


def test_manual_bus_boarding_arrival_and_walk_arrival_need_explicit_place_evidence():
    selected = select()
    boarded = action(selected, "boarded")
    selected = apply_manual(selected, boarded, now=boarded.at)
    assert selected.index == 0 and selected.revision == 1
    arrived = action(selected, "arrived", at=selected.itinerary.steps[0].arrival)
    selected = apply_manual(selected, arrived, now=arrived.at)
    assert selected.index == 1
    walked = action(selected, "walk_arrived", at=selected.itinerary.steps[1].arrival)
    selected = apply_manual(selected, walked, now=walked.at)
    assert selected.index == 2 and selected.status == "active"
    assert len(selected.confirmations) == 3


def test_no_clock_progress_and_unavailable_source_does_not_erase_manual_progress():
    selected = select()
    late = selected.itinerary.steps[0].arrival + timedelta(minutes=5)
    selected = replace(selected, checked_at=NOW - timedelta(days=1))
    assert selected.index == 0 and selected.status == "active"
    boarded = action(selected, "boarded", at=late)
    selected = apply_manual(selected, boarded, now=late)
    assert selected.index == 0
    arrived = action(selected, "arrived", at=late + timedelta(minutes=1))
    selected = apply_manual(selected, arrived, now=arrived.at)
    assert selected.index == 1


def test_duplicate_actions_are_idempotent_but_old_or_reused_callbacks_are_refused():
    selected = select()
    boarded = action(selected, "boarded")
    updated = apply_manual(selected, boarded, now=boarded.at)
    assert apply_manual(updated, boarded, now=boarded.at + timedelta(seconds=1)) == updated
    with pytest.raises(RecoveryActionError):
        apply_manual(updated, replace(boarded, id="new-action"), now=boarded.at)
    with pytest.raises(RecoveryActionError):
        apply_manual(
            updated,
            replace(boarded, place=boarded.place.__class__("other", "other", "GD", 1, 1)),
            now=boarded.at,
        )
    with pytest.raises(RecoveryActionError):
        apply_manual(updated, replace(boarded, selection_id="another"), now=boarded.at)


def test_arrival_without_boarding_wrong_place_and_reversed_confirmation_are_refused():
    selected = select()
    arrived = action(selected, "arrived", at=selected.itinerary.steps[0].arrival)
    with pytest.raises(RecoveryActionError):
        apply_manual(selected, arrived, now=arrived.at)
    boarded = action(selected, "boarded", place=selected.itinerary.steps[0].destination)
    with pytest.raises(RecoveryActionError):
        apply_manual(selected, boarded, now=boarded.at)
    boarded = action(selected, "boarded")
    selected = apply_manual(selected, boarded, now=boarded.at)
    earlier = action(selected, "arrived", at=boarded.at - timedelta(seconds=1))
    with pytest.raises(RecoveryActionError):
        apply_manual(selected, earlier, now=boarded.at)


def test_complete_only_after_every_manual_arrival_and_stop_is_explicit():
    selected = select()
    while selected.status == "active":
        step = selected.itinerary.steps[selected.index]
        if step.mode != "WALK":
            boarded = action(selected, "boarded")
            selected = apply_manual(selected, boarded, now=boarded.at)
        arrival = action(
            selected, "walk_arrived" if step.mode == "WALK" else "arrived", at=step.arrival
        )
        selected = apply_manual(selected, arrival, now=arrival.at)
    assert selected.status == "completed" and selected.index == len(selected.itinerary.steps)
    assert loads(SelectedRecovery, dumps(selected)) == selected
    active = select()
    stopped = RecoveryAction("stop", active.id, active.revision, active.index, "stop", None, NOW)
    result = apply_manual(active, stopped, now=NOW)
    assert result.status == "stopped" and result.index == 0


def test_service_identity_uses_planned_time_and_physical_keys_not_forecasts_or_aliases():
    from train_commute_companion.recovery import RecoveryStep
    from train_commute_companion.serde import decode, encode

    step = offer().itinerary.steps[0]
    changed = replace(
        step,
        departure=step.departure + timedelta(minutes=4),
        arrival=step.arrival + timedelta(minutes=4),
        origin=replace(step.origin, name="A different label", station_code="8400258"),
    )
    assert changed.service_key == step.service_key
    assert (
        replace(
            step, origin=replace(step.origin, longitude=step.origin.longitude + 0.001)
        ).service_key
        != step.service_key
    )
    assert (
        replace(step, origin=replace(step.origin, physical_id="another-stop")).service_key
        != step.service_key
    )
    raw = encode(step)
    raw.pop("planned_departure")
    raw.pop("planned_arrival")
    legacy = decode(RecoveryStep, raw)
    assert legacy.service_key is None and legacy.planned_arrival is None


def test_offer_acknowledgement_cannot_be_reused_after_times_or_source_check_change():
    candidate = offer()
    for changed in (
        replace(candidate, checked_at=NOW - timedelta(seconds=1)),
        replace(
            candidate,
            itinerary=replace(
                candidate.itinerary,
                steps=(
                    replace(
                        candidate.itinerary.steps[0],
                        arrival=candidate.itinerary.steps[0].arrival + timedelta(seconds=1),
                    ),
                    *candidate.itinerary.steps[1:],
                ),
            ),
        ),
    ):
        with pytest.raises(RecoveryActionError, match="reviewed"):
            select(changed, fingerprint=candidate.fingerprint)


def test_manual_confirmation_refuses_future_time_and_old_step_without_advancing():
    selected = select()
    boarded = action(selected, "boarded")
    with pytest.raises(RecoveryActionError, match="time"):
        apply_manual(selected, boarded, now=boarded.at - timedelta(seconds=1))
    selected = apply_manual(selected, boarded, now=boarded.at)
    arrived = action(selected, "arrived", at=selected.itinerary.steps[0].arrival)
    selected = apply_manual(selected, arrived, now=arrived.at)
    with pytest.raises(RecoveryActionError):
        apply_manual(
            selected, replace(arrived, id="another", revision=selected.revision), now=arrived.at
        )
    assert selected.index == 1


def test_selection_rejects_missing_transfer_records_and_missing_time_provenance():
    from train_commute_companion.model import Provenance

    candidate = offer()
    for itinerary in (
        replace(candidate.itinerary, transfers=()),
        replace(
            candidate.itinerary,
            steps=(
                replace(candidate.itinerary.steps[0], departure_provenance=Provenance.MISSING),
                *candidate.itinerary.steps[1:],
            ),
        ),
    ):
        with pytest.raises(RecoveryActionError):
            select(replace(candidate, itinerary=itinerary))


def test_service_key_separates_both_occurrences_of_the_dst_fold():
    from datetime import datetime

    from train_commute_companion.calendar import TZ

    step = offer().itinerary.steps[0]
    first = replace(step, planned_departure=datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=0))
    second = replace(step, planned_departure=datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=1))
    assert first.service_key != second.service_key
    assert first.service_key.planned_departure.utcoffset() == timedelta()


@pytest.mark.parametrize(
    "changes",
    [
        {"acknowledge_planned_times": "yes"},
        {"acknowledge_accessibility_unknown": 1},
        {"generation": True},
        {"route_revision": -1},
        {"require_accessible": 0},
    ],
)
def test_selection_boundary_refuses_malformed_acknowledgements_and_revisions(changes):
    with pytest.raises(RecoveryActionError):
        select(**changes)


def test_source_and_origin_times_use_utc_order_including_missing_or_future_confirmation():
    candidate = offer()
    for changes in (
        {"confirmed_at": NOW + timedelta(seconds=1)},
        {"confirmed_at": NOW - timedelta(minutes=11)},
        {"now": NOW.replace(tzinfo=None)},
    ):
        with pytest.raises(RecoveryActionError):
            select(candidate, **changes)


def test_walk_only_progress_does_not_claim_vehicle_boarding():
    candidate = offer()
    walk = candidate.itinerary.steps[1]
    candidate = replace(
        candidate, itinerary=replace(candidate.itinerary, steps=(walk,), transfers=())
    )
    selected = select(candidate)
    with pytest.raises(RecoveryActionError):
        apply_manual(selected, action(selected, "boarded"), now=walk.departure)
    arrived = action(selected, "walk_arrived", at=walk.arrival)
    result = apply_manual(selected, arrived, now=walk.arrival)
    assert result.status == "completed"


@pytest.mark.parametrize(
    "field,value",
    [("revision", False), ("revision", 0.0), ("step_index", False), ("step_index", 0.0)],
)
def test_manual_callback_revision_and_step_index_require_nonboolean_integers(field, value):
    selected = select()
    boarded = replace(action(selected, "boarded"), **{field: value})
    with pytest.raises(RecoveryActionError):
        apply_manual(selected, boarded, now=boarded.at)


def test_offer_fingerprint_preserves_instants_across_timezone_normalization_and_serde():
    from datetime import UTC

    candidate = offer()
    utc = replace(
        candidate,
        checked_at=candidate.checked_at.astimezone(UTC),
        offered_at=candidate.offered_at.astimezone(UTC),
        source_query=replace(candidate.source_query, at=candidate.source_query.at.astimezone(UTC)),
    )
    assert utc.fingerprint == candidate.fingerprint
    assert loads(RecoveryOffer, dumps(utc)).fingerprint == utc.fingerprint


def test_invalid_callback_numbers_cannot_bypass_validation_as_idempotent_duplicates():
    selected = select()
    boarded = action(selected, "boarded")
    updated = apply_manual(selected, boarded, now=boarded.at)
    with pytest.raises(RecoveryActionError):
        apply_manual(updated, replace(boarded, revision=False), now=boarded.at)
    with pytest.raises(RecoveryActionError):
        apply_manual(updated, replace(boarded, step_index=0.0), now=boarded.at)


def test_offer_fingerprint_distinguishes_dst_fold_instants_and_survives_their_roundtrip():
    from datetime import UTC, datetime

    from train_commute_companion.calendar import TZ

    candidate = offer()
    first_at = datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=0)
    second_at = first_at.replace(fold=1)
    first = replace(candidate, checked_at=first_at, offered_at=first_at)
    second = replace(candidate, checked_at=second_at, offered_at=second_at)
    assert first.fingerprint != second.fingerprint
    assert loads(RecoveryOffer, dumps(first)).fingerprint == first.fingerprint
    assert loads(RecoveryOffer, dumps(second)).fingerprint == second.fingerprint
    assert (
        replace(
            first, checked_at=first_at.astimezone(UTC), offered_at=first_at.astimezone(UTC)
        ).fingerprint
        == first.fingerprint
    )


def test_all_step_instants_are_canonicalized_in_offer_fingerprint():
    from datetime import UTC

    candidate = offer()
    steps = tuple(
        replace(
            step,
            departure=step.departure.astimezone(UTC),
            arrival=step.arrival.astimezone(UTC),
            planned_departure=step.planned_departure.astimezone(UTC),
            planned_arrival=step.planned_arrival.astimezone(UTC),
        )
        for step in candidate.itinerary.steps
    )
    utc = replace(candidate, itinerary=replace(candidate.itinerary, steps=steps))
    assert utc.fingerprint == candidate.fingerprint
    assert loads(RecoveryOffer, dumps(utc)).fingerprint == utc.fingerprint


def test_explicit_recovery_progress_preserves_manual_confirmation_guards():
    from train_commute_companion.recovery_state import RecoveryProgress

    selected = select()
    assert selected.progress_phase is RecoveryProgress.WAITING_TO_BOARD
    with pytest.raises(RecoveryActionError, match="arrival"):
        apply_manual(selected, action(selected, "arrived"), now=NOW + timedelta(hours=3))
    boarded = apply_manual(selected, action(selected, "boarded"), now=NOW + timedelta(hours=3))
    assert boarded.progress_phase is RecoveryProgress.ABOARD
    assert boarded.next_action == "arrived"
    arrived = apply_manual(boarded, action(boarded, "arrived"), now=NOW + timedelta(hours=3))
    assert arrived.progress_phase is RecoveryProgress.WALKING
    assert arrived.next_action == "walk_arrived"
    assert loads(SelectedRecovery, dumps(arrived)).progress_phase is RecoveryProgress.WALKING


def test_monitoring_transition_cannot_erase_required_review_or_move_physical_step():
    from train_commute_companion.recovery_state import RecoverySourcePhase

    selected = select()
    changed = selected.update_monitoring(
        RecoverySourcePhase.CHANGED,
        error="changed route",
        checked_at=NOW,
        attempted_at=NOW,
    )
    assert changed.review_required and changed.source_phase is RecoverySourcePhase.CHANGED
    recovered = changed.update_monitoring(
        RecoverySourcePhase.CURRENT,
        error=None,
        checked_at=NOW + timedelta(minutes=1),
        attempted_at=NOW + timedelta(minutes=1),
    )
    assert recovered.review_required and recovered.source_phase is RecoverySourcePhase.CHANGED
    assert recovered.index == selected.index and recovered.confirmations == selected.confirmations
    assert not recovered.fields(NOW + timedelta(minutes=1))["source_fresh"]


def test_recovery_offer_phase_blocks_stale_source_and_wrong_occurrence():
    from train_commute_companion.recovery_state import RecoveryOfferPhase

    candidate = offer()
    assert candidate.phase_at(NOW, 3, 2) is RecoveryOfferPhase.CURRENT
    assert candidate.phase_at(NOW, 4, 2) is RecoveryOfferPhase.STALE_CONTEXT
    assert candidate.phase_at(NOW + timedelta(minutes=5), 3, 2) is RecoveryOfferPhase.STALE_SOURCE
    assert candidate.phase_at(NOW - timedelta(seconds=1), 3, 2) is RecoveryOfferPhase.STALE_SOURCE
    naive = NOW.replace(tzinfo=None)
    assert (
        replace(candidate, checked_at=naive).phase_at(naive, 3, 2)
        is RecoveryOfferPhase.STALE_SOURCE
    )

"""Transition contracts for the independent journey state dimensions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from builders import AMS
from lifecycle_fixtures import L1, L2, RIDE, at, journey, occ, polls
from lookahead_fixtures import MON, NEXT_TUE, confirmed

from train_commute_companion.lifecycle import is_resolved
from train_commute_companion.lifecycle_end import may_archive
from train_commute_companion.location import next_zone, seed_zone
from train_commute_companion.model import AdviceRecord, DirectionState, LegState
from train_commute_companion.serde import encode


def test_completed_occurrence_with_retained_train_is_still_followed():
    from train_commute_companion.state import ArchivePhase, OccurrencePhase

    current = replace(occ(journey([L1, L2], RIDE)), direction_state=DirectionState.DONE)
    retained = replace(current, retained=(current.lock,))
    assert current.phase is OccurrencePhase.RESOLVED
    assert retained.phase is OccurrencePhase.FOLLOWING
    assert not is_resolved(retained)
    assert retained.archive_phase(at("19:00"), 0) is ArchivePhase.FOLLOWING
    assert not may_archive(retained, at("19:00"), 0)
    assert current.archive_phase(at("19:00"), 1) is ArchivePhase.WARNING_PENDING


def test_arrival_progress_is_not_serialized_as_a_second_state():
    from train_commute_companion.state import ArrivalPhase

    initial = occ(journey([L1, L2], RIDE))
    candidate = polls(initial, "09:29")
    assert candidate.lock.legs[-1].arrival_phase is ArrivalPhase.STABILIZING
    accepted = polls(candidate, "09:33")
    assert accepted.lock.legs[-1].arrival_phase is ArrivalPhase.WATCHING
    assert "arrival_phase" not in encode(accepted.lock.legs[-1])
    blocked = replace(candidate.lock.legs[-1], state=LegState.CANCELLED)
    with pytest.raises(ValueError, match="arrival"):
        blocked.accept_arrival(at("09:33"))


def test_arrival_candidate_must_exist_before_acceptance():
    leg = journey([L1], RIDE).legs[0]
    with pytest.raises(ValueError, match="arrival"):
        leg.accept_arrival(at("09:33"))


def test_frozen_empty_baseline_is_not_an_unhydrated_baseline():
    from train_commute_companion.state import BaselineBinding

    initial = occ(journey([L1], RIDE))
    assert initial.baseline_binding is BaselineBinding.UNHYDRATED
    assert replace(initial, baseline_frozen=True).baseline_binding is BaselineBinding.EMPTY
    assert "baseline_binding" not in encode(initial)


def test_baseline_phase_separates_confirmation_and_effective_date():
    from train_commute_companion.state import BaselinePhase

    baseline = confirmed(2, NEXT_TUE)
    assert baseline.phase_on(MON) is BaselinePhase.CONFIRMED_FUTURE
    assert baseline.phase_on(NEXT_TUE) is BaselinePhase.CONFIRMED_CURRENT
    provisional = replace(baseline, confirmed=False)
    assert provisional.phase_on(MON) is BaselinePhase.PROVISIONAL_FUTURE
    assert provisional.phase_on(NEXT_TUE) is BaselinePhase.PROVISIONAL_CURRENT


def test_advice_withdrawal_preserves_whether_user_received_it():
    from train_commute_companion.model import AdvicePhase

    train = L1.train
    pending = AdviceRecord(train, at("07:30"), False, delivered=False)
    assert pending.phase is AdvicePhase.PENDING
    assert pending.withdraw().phase is AdvicePhase.WITHDRAWN_UNSENT
    delivered = pending.accept_delivery()
    assert delivered.phase is AdvicePhase.DELIVERED
    assert delivered.withdraw().phase is AdvicePhase.WITHDRAWN_DELIVERED
    late_receipt = pending.withdraw().accept_delivery()
    assert late_receipt.phase is AdvicePhase.WITHDRAWN_DELIVERED
    with pytest.raises(ValueError, match="advice"):
        AdviceRecord(None, None, False, delivered=False).accept_delivery()


def test_location_report_timer_compares_instants_across_fall_back():
    first = datetime(2026, 10, 25, 2, 59, tzinfo=AMS, fold=0)
    after = datetime(2026, 10, 25, 2, 1, tzinfo=AMS, fold=1)
    assert after.astimezone(UTC) - first.astimezone(UTC) == timedelta(minutes=2)
    assert next_zone(seed_zone("UT", first), "UT", after)[1] == [("UT", "report")]


def test_location_report_phase_is_consumed_by_timer_transition():
    from train_commute_companion.location import ZoneReportPhase

    track = seed_zone("UT", at("08:30"))
    assert track.phase_at(at("08:31")) is ZoneReportPhase.THROTTLED
    assert next_zone(track, "UT", at("08:31"))[1] == []
    assert track.phase_at(at("08:32")) is ZoneReportPhase.READY
    assert next_zone(track, "UT", at("08:32"))[1] == [("UT", "report")]


def test_endpoint_commitment_cannot_move_after_onward_departure():
    from train_commute_companion.state import EndpointPhase

    leg = replace(journey([L1], RIDE).legs[0], alight_frozen=True)
    moved = leg.move_endpoint("OTHER", "OTHER")
    assert moved.endpoint_phase is EndpointPhase.FROZEN
    assert moved.alight_stop == leg.alight_stop
    assert moved.served_endpoint == "OTHER"


def test_explicit_lock_decision_rejects_stale_and_protected_user_choice():
    from train_commute_companion.lifecycle_lock import LockDecision, lock_decision, write_lock
    from train_commute_companion.state import Commitment, LockSource

    chosen = replace(journey([L1, L2], RIDE), source=LockSource.CHOOSE)
    state = occ(chosen)
    automatic = replace(chosen, source=LockSource.AUTO, commitment=Commitment.INTENDED)
    assert lock_decision(chosen, automatic, chosen) is LockDecision.USER_CHOICE
    assert write_lock(state, automatic, expect=chosen, now=at("07:00")) is None
    other = replace(chosen, source=LockSource.RESTORE)
    assert lock_decision(chosen, other, automatic) is LockDecision.STALE
    assert write_lock(state, other, expect=automatic, now=at("07:00")) is None


def test_warning_marks_keep_warning_and_escalation_as_distinct_phases():
    from train_commute_companion.lookahead_probe import (
        WarningPhase,
        is_warning,
        loud_mark,
        mark_phase,
        owned_mark,
        restored_mark,
        warn_mark,
    )

    assert mark_phase(owned_mark(warn_mark("x"), "owner")) is WarningPhase.WARNED
    assert mark_phase(loud_mark("x")) is WarningPhase.ESCALATED
    assert mark_phase(restored_mark("x")) is WarningPhase.RESTORED
    assert is_warning(warn_mark("x")) and not is_warning(loud_mark("x"))


def test_accept_arrival_rejects_a_candidate_without_the_stability_interval():
    initial = occ(journey([L1, L2], RIDE))
    candidate = polls(initial, "09:29").lock.legs[-1]
    with pytest.raises(ValueError, match="stable"):
        candidate.accept_arrival(at("09:32"))
    assert candidate.accept_arrival(at("09:33")).state is LegState.DONE


def test_baseline_confirmation_keeps_its_schedule_identity_and_scope():
    from train_commute_companion.state import BaselinePhase

    provisional = replace(confirmed(2, NEXT_TUE), confirmed=False, commute_weekday=2)
    saved = provisional.confirm()
    assert saved.phase_on(MON) is BaselinePhase.CONFIRMED_FUTURE
    assert saved.version == provisional.version and saved.signature == provisional.signature
    assert saved.effective_from == provisional.effective_from and saved.commute_weekday == 2
    assert saved.confirm() == saved

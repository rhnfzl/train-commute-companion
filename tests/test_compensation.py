"""Only explicit arrivals can produce one optional saved-plan comparison reminder."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from builders import make_connection, make_leg
from runtime_rig import Rig, at

from train_commute_companion.compensation import ArrivalError, confirm, reference_from_connection
from train_commute_companion.compensation_state import JourneyRecord
from train_commute_companion.model import Direction, OccurrenceKey
from train_commute_companion.serde import dumps, loads

KEY = OccurrenceKey("p1", Direction.OUTBOUND, date(2026, 10, 27))


def record():
    plan = reference_from_connection(make_connection([make_leg("AAA", "RTD", "08:12", "09:00")]))
    assert plan is not None
    return JourneyRecord(KEY, "RTD", plan, 1)


@pytest.mark.parametrize("seconds,expected", [(-61, -1), (-1, 0), (1799, 29), (1800, 30)])
def test_comparison_uses_completed_minutes_in_both_directions(seconds, expected):
    old = record()
    arrival = old.plan.planned_arrival + timedelta(seconds=seconds)
    updated = confirm(
        old, "RTD", arrival, now=at("10:00"), expected_revision=0, action_id="precision"
    )
    assert updated.delay_minutes == expected


def test_explicit_arrival_correction_duplicate_and_roundtrip():
    old = record()
    updated = confirm(
        old, "RTD", at("09:35"), now=at("09:40"), expected_revision=0, action_id="one"
    )
    assert updated.delay_minutes == 35
    assert updated.arrival_source == "user"
    assert (
        confirm(updated, "RTD", at("09:35"), now=at("10:00"), expected_revision=0, action_id="one")
        == updated
    )
    assert loads(JourneyRecord, dumps(updated)) == updated
    corrected = confirm(
        updated, "RTD", at("09:05"), now=at("10:00"), expected_revision=1, action_id="two"
    )
    assert corrected.revision == 2 and corrected.delay_minutes == 5
    assert corrected.plan == old.plan
    with pytest.raises(ArrivalError):
        confirm(updated, "RTD", at("09:36"), now=at("10:00"), expected_revision=1, action_id="one")


@pytest.mark.parametrize(
    "destination,arrived,revision",
    [
        ("UT", at("09:35"), 0),
        ("RTD", at("10:00"), 0),
        ("RTD", at("08:00"), 0),
        ("RTD", datetime(2026, 10, 27, 9, 35), 0),
        ("RTD", at("09:35"), False),
        ("RTD", at("09:35"), 0.0),
    ],
)
def test_invalid_confirmation_refused(destination, arrived, revision):
    with pytest.raises(ArrivalError):
        confirm(
            record(),
            destination,
            arrived,
            now=at("09:40"),
            expected_revision=revision,
            action_id="x",
        )


def test_unknown_original_plan_requires_explicit_reference():
    old = replace(record(), plan=None)
    with pytest.raises(ArrivalError):
        confirm(old, "RTD", at("09:35"), now=at("09:40"), expected_revision=0, action_id="x")
    updated = confirm(
        old,
        "RTD",
        at("09:35"),
        now=at("09:40"),
        expected_revision=0,
        action_id="x",
        original_planned_arrival=at("09:00"),
    )
    assert updated.plan.source == "user"


def test_dst_arithmetic_uses_instants():
    zone = ZoneInfo("Europe/Amsterdam")
    first = datetime(2026, 10, 25, 2, 10, tzinfo=zone, fold=0)
    second = datetime(2026, 10, 25, 2, 5, tzinfo=zone, fold=1)
    old = replace(
        record(),
        key=replace(KEY, service_date=date(2026, 10, 25)),
        plan=replace(
            record().plan, planned_departure=first - timedelta(minutes=20), planned_arrival=first
        ),
    )
    updated = confirm(
        old, "RTD", second, now=second.astimezone(UTC), expected_revision=0, action_id="dst"
    )
    assert updated.delay_minutes == 55


async def arrived_world(tmp_path, *, enabled=True):
    from runtime_rig import PROFILE

    rig = Rig(tmp_path, now="08:05")
    text = PROFILE[: PROFILE.index('  - id: "p2"')]
    if enabled:
        text += "    alerts: {compensation_reminder: true}\n"
    rig.write(text=text)
    await rig.rt.start()
    return rig


async def test_runtime_reference_preserved_across_replacement_archive_and_restart(tmp_path):
    from train_commute_companion.model import okey
    from train_commute_companion.state import DirectionState

    rig = await arrived_world(tmp_path)
    original = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    assert original.plan is not None and original.plan.planned_arrival == at("09:24")
    assert original.plan.checked_at is not None
    state = rig.rt.state.occurrences[okey(original.key)]
    rig.occ_set(
        replace(
            state,
            direction_state=DirectionState.DONE,
            lock=replace(state.lock, planned_final_arrival=at("10:24")),
        ),
        key=okey(original.key),
    )
    rig.clock.t = at("23:00")
    await rig.rt.watchdog_once()
    await rig.restart()
    current = next(r for r in rig.rt.arrival_records() if r.key == original.key)
    assert current.plan == original.plan
    result = await rig.rt.confirm_arrival(
        original.key, "RTD", at("10:00"), expected_revision=0, action_id="archived"
    )
    assert isinstance(result, JourneyRecord) and result.delay_minutes == 36


async def test_runtime_failed_write_no_later_activation_once_only_and_correction(tmp_path):
    from train_commute_companion.messages import Refused
    from train_commute_companion.model import EventKind

    rig = await arrived_world(tmp_path)
    original = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    rig.clock.t = at("10:05")
    rig.store.fail = True
    refused = await rig.rt.confirm_arrival(
        original.key, "RTD", at("10:00"), expected_revision=0, action_id="first"
    )
    assert isinstance(refused, Refused)
    rig.store.fail = False
    await rig.restart()
    assert next(r for r in rig.rt.arrival_records() if r.key == original.key).arrived_at is None
    rig.notifier.fails = lambda e: e.kind is EventKind.COMPENSATION_REMINDER
    first = await rig.rt.confirm_arrival(
        original.key, "RTD", at("10:00"), expected_revision=0, action_id="first"
    )
    assert isinstance(first, JourneyRecord) and first.reminder_created
    assert any(o.event.kind is EventKind.COMPENSATION_REMINDER for o in rig.rt.state.obligations)
    corrected = await rig.rt.confirm_arrival(
        original.key, "RTD", at("09:30"), expected_revision=1, action_id="correct"
    )
    assert isinstance(corrected, JourneyRecord)
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    assert not any(
        o.event.kind is EventKind.COMPENSATION_REMINDER for o in rig.rt.state.obligations
    )
    again = await rig.rt.confirm_arrival(
        original.key, "RTD", at("10:01"), expected_revision=2, action_id="correct-again"
    )
    assert isinstance(again, JourneyRecord)
    await rig.restart()
    assert not any(e.kind is EventKind.COMPENSATION_REMINDER for e in rig.rt.events_log)


async def test_no_forecast_only_reminder_and_preference_default_off(tmp_path):
    from train_commute_companion.model import EventKind

    rig = await arrived_world(tmp_path, enabled=False)
    rig.clock.t = at("10:05")
    await rig.rt.poll_once()
    assert not any(e.kind is EventKind.COMPENSATION_REMINDER for e in rig.rt.events_log)
    original = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    result = await rig.rt.confirm_arrival(
        original.key, "RTD", at("10:00"), expected_revision=0, action_id="no-opt-in"
    )
    assert isinstance(result, JourneyRecord) and not result.reminder_created


@pytest.mark.parametrize("value", ["1", "null", "'true'", "[]"])
def test_reminder_preference_requires_boolean(value):
    from runtime_rig import DAY, PROFILE

    from train_commute_companion.profile import load_config

    text = (
        PROFILE[: PROFILE.index('  - id: "p2"')]
        + f"    alerts: {{compensation_reminder: {value}}}\n"
    )
    config, errors = load_config(text, None, None, DAY)
    assert config is None and errors


def test_arrival_action_id_distinguishes_dst_fold_instants():
    zone = ZoneInfo("Europe/Amsterdam")
    first = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    old = replace(
        record(),
        key=replace(KEY, service_date=date(2026, 10, 25)),
        plan=replace(
            record().plan, planned_departure=first - timedelta(hours=1), planned_arrival=first
        ),
    )
    saved = confirm(old, "RTD", first, now=second, expected_revision=0, action_id="fold")
    with pytest.raises(ArrivalError):
        confirm(saved, "RTD", second, now=second, expected_revision=1, action_id="fold")


async def test_first_broken_usual_preserved_before_same_poll_replacement(tmp_path):
    from runtime_rig import PROFILE

    rig = Rig(tmp_path, now="08:05")
    rig.write(text=PROFILE[: PROFILE.index('  - id: "p2"')])
    original = make_connection(
        [
            make_leg("AAA", "UT", "08:12", "08:40", cancelled=True),
            make_leg("UT", "RTD", "08:50", "09:24"),
        ]
    )
    replacement = make_connection(
        [
            make_leg("AAA", "UT", "08:30", "09:00", number="2345"),
            make_leg("UT", "RTD", "09:10", "09:50", number="6789"),
        ]
    )
    rig.provider.world = [original, replacement]
    await rig.rt.start()
    saved = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    assert saved.plan is not None and saved.plan.planned_arrival == at("09:24")
    current = next(s for s in rig.rt.state.occurrences.values() if s.key == saved.key)
    assert current.lock.planned_final_arrival == at("09:50")


async def test_missing_usual_does_not_turn_fallback_into_original_reference(tmp_path):
    from runtime_rig import PROFILE

    rig = Rig(tmp_path, now="08:05")
    rig.write(text=PROFILE[: PROFILE.index('  - id: "p2"')])
    rig.provider.world = [
        make_connection(
            [make_leg("AAA", "UT", "08:30", "09:00"), make_leg("UT", "RTD", "09:10", "09:50")]
        )
    ]
    await rig.rt.start()
    saved = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    assert saved.plan is None


async def test_legacy_lock_without_original_reference_stays_unknown(tmp_path):
    from train_commute_companion.model import okey

    rig = await arrived_world(tmp_path)
    saved = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    state = rig.rt.state.occurrences[okey(saved.key)]
    legacy = replace(state, original_plan=None)
    rig.rt._writer.commit_strict(
        replace(
            rig.rt.state,
            occurrences={**rig.rt.state.occurrences, okey(saved.key): legacy},
            journey_records={},
        )
    )
    await rig.rt.poll_once()
    saved = next(r for r in rig.rt.arrival_records() if r.key == saved.key)
    assert saved.plan is None


def test_service_date_boundary_is_amsterdam_even_when_clock_is_utc():
    zone = ZoneInfo("Europe/Amsterdam")
    plan = datetime(2026, 10, 27, 0, 20, tzinfo=zone)
    arrived = datetime(2026, 10, 27, 0, 30, tzinfo=zone)
    unknown = replace(record(), plan=None)
    saved = confirm(
        unknown,
        "RTD",
        arrived,
        now=arrived.astimezone(UTC),
        expected_revision=0,
        action_id="midnight",
        original_planned_arrival=plan,
    )
    assert saved.delay_minutes == 10
    with pytest.raises(ArrivalError):
        confirm(
            unknown,
            "RTD",
            datetime(2025, 10, 27, tzinfo=zone),
            now=arrived.astimezone(UTC),
            expected_revision=0,
            action_id="old",
            original_planned_arrival=plan,
        )


async def test_retention_holds_queued_record_then_prunes_after_120_days(tmp_path):
    from train_commute_companion.model import EventKind, okey

    rig = await arrived_world(tmp_path)
    old = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    rig.clock.t = at("10:05")
    rig.notifier.fails = lambda e: e.kind is EventKind.COMPENSATION_REMINDER
    await rig.rt.confirm_arrival(old.key, "RTD", at("10:00"), expected_revision=0, action_id="held")
    future = rig.clock.now() + timedelta(days=121)
    archived = rig.rt._archived(replace(rig.rt.state, occurrences={}), future)
    assert okey(old.key) in archived.journey_records
    cleared = rig.rt._archived(replace(archived, obligations=(), webhook_copies=()), future)
    assert okey(old.key) not in cleared.journey_records


async def test_routine_category_suppresses_reminder_without_repeat_on_correction(tmp_path):
    from runtime_rig import PROFILE

    from train_commute_companion.model import EventKind

    rig = Rig(tmp_path, now="08:05")
    rig.write(
        text=PROFILE[: PROFILE.index('  - id: "p2"')]
        + "    alerts: {compensation_reminder: true, disabled_categories: [routine]}\n"
    )
    await rig.rt.start()
    old = next(r for r in rig.rt.arrival_records() if r.key.direction is Direction.OUTBOUND)
    rig.clock.t = at("10:05")
    saved = await rig.rt.confirm_arrival(
        old.key, "RTD", at("10:00"), expected_revision=0, action_id="suppressed"
    )
    assert isinstance(saved, JourneyRecord) and saved.reminder_created
    assert not any(e.kind is EventKind.COMPENSATION_REMINDER for e in rig.notifier.attempts)
    assert not any(
        o.event.kind is EventKind.COMPENSATION_REMINDER for o in rig.rt.state.obligations
    )


def test_arrival_record_phases_and_reminder_latch_preserve_immutable_plan():
    from train_commute_companion.compensation_state import (
        ArrivalRecordPhase,
        PlanPhase,
        ReminderPhase,
    )

    initial = record()
    assert initial.plan_phase is PlanPhase.CAPTURED
    assert initial.arrival_phase is ArrivalRecordPhase.UNCONFIRMED
    first = confirm(
        initial, "RTD", at("09:35"), now=at("10:00"), expected_revision=0, action_id="first"
    )
    assert first.arrival_phase is ArrivalRecordPhase.CONFIRMED
    assert first.reminder_phase is ReminderPhase.UNCREATED and first.reminder_due(True)
    queued = first.queue_reminder()
    assert queued.reminder_phase is ReminderPhase.QUEUED and not queued.reminder_due(True)
    corrected = confirm(
        queued, "RTD", at("09:40"), now=at("10:00"), expected_revision=1, action_id="second"
    )
    assert corrected.arrival_phase is ArrivalRecordPhase.CORRECTED
    assert corrected.plan == initial.plan and not corrected.reminder_due(True)
    with pytest.raises(ValueError, match="arrival"):
        initial.queue_reminder()
    assert loads(JourneyRecord, dumps(corrected)) == corrected

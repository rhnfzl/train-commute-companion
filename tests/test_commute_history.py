from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
import runtime_seam as seam
from runtime_rig import PROFILE, Rig, at

from train_commute_companion.calendar import TZ
from train_commute_companion.history import (
    HISTORY_DAYS,
    JourneyHistory,
    commute_stats,
    retained_history,
)
from train_commute_companion.model import (
    Direction,
    DirectionState,
    EventKind,
    LegState,
    OccurrenceKey,
    okey,
)
from train_commute_companion.runtime import Runtime

DAY = date(2026, 11, 16)


def journey(day: date, delay: int | None, **flags: bool) -> JourneyHistory:
    plan = datetime(day.year, day.month, day.day, 9, 0, tzinfo=TZ)
    return JourneyHistory(
        OccurrenceKey("office", Direction.OUTBOUND, day),
        plan - timedelta(hours=1),
        plan,
        None if delay is None else plan + timedelta(minutes=delay),
        cancelled=flags.get("cancelled", False),
        moved=flags.get("moved", False),
        missed=flags.get("missed", False),
    )


def test_stats_use_only_known_ns_arrivals_for_delay_and_on_time_rate():
    rows = [
        journey(DAY, -3),
        journey(DAY + timedelta(days=1), 9, moved=True),
        journey(DAY + timedelta(days=2), None, cancelled=True, missed=True),
        journey(DAY + timedelta(days=3), 2),
        journey(DAY + timedelta(days=4), 3),
        journey(DAY - timedelta(days=1), 80),
    ]

    stats = commute_stats(rows, DAY, DAY + timedelta(days=4))

    assert stats.journeys == 5 and stats.arrivals == 4
    assert stats.on_time == 2 and stats.on_time_percent == 50
    assert stats.average_delay_min == 3.5 and stats.worst_weekday == 2
    assert stats.cancelled == stats.moved == stats.missed == 1


def test_history_delay_uses_instants_across_the_repeated_hour():
    day = date(2026, 10, 25)
    first = datetime(2026, 10, 25, 2, 20, tzinfo=TZ)
    row = replace(journey(day, 0), planned_arrival=first, ns_arrival=first.replace(fold=1))
    assert row.delay_seconds == 3600


def test_retention_keeps_exactly_the_configured_calendar_span():
    expired = journey(DAY - timedelta(days=HISTORY_DAYS), None)
    first = journey(DAY - timedelta(days=HISTORY_DAYS - 1), None)
    last = journey(DAY, None)
    rows = tuple(retained_history([expired, first, last], DAY))
    assert rows == (first, last)
    stats = commute_stats(rows, DAY, DAY)
    assert stats.on_time_percent is stats.average_delay_min is stats.worst_weekday is None


@pytest.mark.parametrize(
    ("enabled", "evidence"),
    [
        (False, "destination"),
        (True, "destination"),
        (True, "short"),
        (True, "reopened"),
        (True, "skipped"),
    ],
)
async def test_archive_uses_current_destination_evidence(tmp_path, enabled, evidence):
    rig = Rig(tmp_path, now="08:05")
    text = (
        PROFILE.split('  - id: "p2"')[0]
        + f"    alerts: {{commute_stats: {str(enabled).lower()}}}\n"
    )
    rig.write(text=text)
    await rig.rt.start()
    (record,) = rig.rt.arrival_records()
    key = okey(record.key)
    state = rig.rt.state.occurrences[key]
    lock = state.lock
    assert lock is not None
    last = replace(
        lock.legs[-1],
        state=LegState.RIDING if evidence == "reopened" else LegState.DONE,
        alight_stop="BBB" if evidence == "short" else record.destination,
        accept_value=at("09:33") if evidence != "reopened" else None,
    )
    rig.occ_set(
        replace(
            state,
            lock=replace(lock, legs=(*lock.legs[:-1], last)),
            direction_state=DirectionState.DONE,
            history_flags=frozenset({"cancelled", "moved", "missed"}),
        ),
        key=key,
    )
    seam.seed_state(rig.rt, journey_records={key: replace(record, train_arrival=at("09:33"))})
    if evidence == "skipped":
        rig.clock.set("09:34")
        assert (
            await rig.rt.not_travelling(
                record.key.profile_id, record.key.service_date, record.key.direction
            )
            is None
        )
        assert rig.rt.state.occurrences[key].not_travelling
    rig.clock.set("23:00")
    await rig.rt.watchdog_once()
    await rig.restart()

    assert key not in rig.rt.state.occurrences
    assert record.key in {saved.key for saved in rig.rt.arrival_records()}
    recorded = enabled and evidence != "skipped"
    assert bool(rig.rt.state.commute_history) == recorded
    summary = rig.rt.commute_stats()
    assert summary.journeys == int(recorded)
    accepted = enabled and evidence == "destination"
    if recorded:
        assert summary.average_delay_min == (9 if accepted else None)
        assert summary.arrivals == int(accepted)
        assert summary.cancelled == summary.moved == summary.missed == 1
    notes = [e for e in rig.notifier.attempts if e.kind is EventKind.AFTER_TRIP_NOTE]
    assert len(notes) == int(accepted)


async def test_failed_reopen_save_prevents_archiving_the_old_arrival(tmp_path):
    rig = Rig(tmp_path, now="08:05")
    rig.write(text=PROFILE.split('  - id: "p2"')[0])
    await rig.rt.start()
    (record,) = rig.rt.arrival_records()
    key = okey(record.key)
    state = rig.rt.state.occurrences[key]
    lock = state.lock
    assert lock is not None
    last = replace(
        lock.legs[-1],
        state=LegState.DONE,
        accept_value=at("09:24"),
        watch_until=at("09:39"),
    )
    completed = replace(
        state,
        lock=replace(lock, legs=(*lock.legs[:-1], last)),
        direction_state=DirectionState.DONE,
    )
    rig.occ_set(completed, key=key)
    seam.save_state(rig.rt)
    rig.store.fail = True
    reopened = replace(last, state=LegState.RIDING, accept_value=None, watch_until=None)
    rig.occ_set(replace(completed, lock=replace(lock, legs=(*lock.legs[:-1], reopened))), key=key)
    seam.save_state(rig.rt)
    rig.clock.set("12:00")
    await rig.rt.watchdog_once()
    rig.store.fail = False
    await rig.rt.lookahead_once()
    await rig.restart()
    await rig.rt.watchdog_once()
    assert rig.rt.commute_stats().arrivals == 0
    assert all(e.kind is not EventKind.AFTER_TRIP_NOTE for e in rig.notifier.attempts)


async def test_sunday_recap_sends_once_and_deletion_survives_restart(tmp_path):
    rig = Rig(tmp_path, now="18:00")
    rig.write(text=PROFILE.split('  - id: "p2"')[0] + "    alerts: {alert_history: true}\n")
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        primary_destination=lambda: "test-phone",
    )
    await rig.rt.start()
    row = journey(DAY, 9)
    row = replace(row, key=replace(row.key, profile_id="p1"))
    key = okey(row.key)
    seam.seed_state(rig.rt, commute_history={key: row})
    seam.save_state(rig.rt)
    sunday = DAY + timedelta(days=6)

    rig.clock.t = at("18:59", sunday)
    await rig.rt.watchdog_once()
    rig.clock.t = at("19:02", sunday)
    await rig.rt.watchdog_once()
    await rig.restart()
    await rig.rt.watchdog_once()
    (recap,) = [e for e in rig.notifier.attempts if e.kind is EventKind.WEEKLY_RECAP]
    assert "Average delay 9 minutes" in recap.text
    assert recap.severity.value == "silent"
    assert not any(row.kind is EventKind.WEEKLY_RECAP for row in rig.rt.state.alert_history)

    assert rig.rt.clear_commute_history()
    await rig.restart()
    assert not rig.rt.state.commute_history
    assert not rig.rt.state.commute_recap_weeks
    assert rig.rt.commute_stats().journeys == 0

from dataclasses import replace
from datetime import timedelta

import pytest
from builders import make_connection, make_leg
from runtime_rig import DAY, PROFILE, Rig, at, started
from runtime_seam import seed_state

from train_commute_companion.compensation_state import ArrivalConfirmation
from train_commute_companion.model import Direction, DirectionState
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import OccurrenceState

OUT = "p1|outbound|2026-10-27"
BACK = "p1|return|2026-10-27"


async def test_explicit_travelling_restores_skipped_journey_durably(tmp_path):
    rig = await started(tmp_path)
    before = rig.occ()
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND) is None
    after = rig.occ()
    assert not after.not_travelling and after.direction_state is before.direction_state
    assert after.lock == before.lock and after.zone_reports == before.zone_reports
    assert after.generation > before.generation
    assert rig.store.load().occurrences[OUT] == after
    await rig.restart()
    assert not rig.occ().not_travelling


async def test_whole_day_restores_upcoming_return_atomically(tmp_path):
    rig = await started(tmp_path)
    await rig.rt.not_travelling("p1", DAY, whole_day=True)
    assert rig.occ(BACK).not_travelling
    assert await rig.rt.travelling("p1", DAY, whole_day=True) is None
    assert all(not rig.occ(k).not_travelling for k in (OUT, BACK))


async def test_active_journey_and_pre_window_travelling_are_idempotent(tmp_path):
    rig = await started(tmp_path, "05:00")
    before = rig.rt.state
    assert await rig.rt.travelling("p1", DAY, whole_day=True) is None
    assert rig.rt.state == before
    rig.clock.set("07:50")
    await rig.rt.poll_once()
    before = rig.occ()
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND) is None
    assert rig.occ() == before


@pytest.mark.parametrize("state", [DirectionState.DONE, DirectionState.EXPIRED])
async def test_skip_after_completed_journey_never_resurrects_it(tmp_path, state):
    rig = await started(tmp_path)
    rig.occ_set(replace(rig.occ(), direction_state=state))
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    skipped = rig.occ()
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND) is None
    assert rig.occ() == skipped


async def test_failed_resume_write_leaves_skip_and_does_not_queue_resume(tmp_path):
    rig = await started(tmp_path)
    await rig.rt.not_travelling("p1", DAY, whole_day=True)
    before = dict(rig.rt.state.occurrences)
    rig.store.fail = True
    reason = await rig.rt.travelling("p1", DAY, whole_day=True)
    assert reason is not None and "saved" in reason.text
    assert rig.rt.state.occurrences == before
    rig.store.fail = False
    await rig.rt.watchdog_once()
    assert all(rig.occ(k).not_travelling for k in (OUT, BACK))


@pytest.mark.parametrize("day_delta", [-1, 1])
async def test_wrong_service_day_never_resumes_current_skip(tmp_path, day_delta):
    rig = await started(tmp_path)
    await rig.rt.not_travelling("p1", DAY, whole_day=True)
    before = dict(rig.rt.state.occurrences)
    assert await rig.rt.travelling("p1", DAY + timedelta(days=day_delta), whole_day=True)
    assert rig.rt.state.occurrences == before


async def test_date_rollover_and_closed_window_never_resurrect_old_skip(tmp_path):
    rig = await started(tmp_path)
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    rig.clock.set("12:00")
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND)
    rig.clock.t = at("07:50", DAY + timedelta(days=1))
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND)
    assert rig.occ() is None or rig.occ().not_travelling


async def test_legacy_skip_without_previous_state_is_refused(tmp_path):
    rig = await started(tmp_path)
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    old = encode(rig.occ())
    old.pop("skipped_direction_state")
    rig.occ_set(decode(OccurrenceState, old))
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND)
    assert rig.occ().not_travelling


async def test_whole_day_uses_next_day_return_offset(tmp_path):
    rig = Rig(tmp_path)
    rig.write(
        text=PROFILE.replace(
            'return: {first_train: "17:52", buffer_min: 5}',
            'return: {first_train: "00:52", buffer_min: 5, day_offset: 1}',
        )
    )
    await rig.rt.start()
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND, whole_day=True)
    back = "p1|return|2026-10-28"
    assert rig.occ(back).not_travelling
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND, whole_day=True) is None
    assert not rig.occ(back).not_travelling


async def test_unknown_profile_is_refused(tmp_path):
    rig = await started(tmp_path)
    assert await rig.rt.travelling("missing", DAY, whole_day=True)


async def test_unwritten_skip_is_not_acknowledged_as_already_travelling(tmp_path):
    rig = await started(tmp_path)
    rig.store.fail = True
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    assert not rig.occ().not_travelling
    reason = await rig.rt.travelling("p1", DAY, Direction.OUTBOUND)
    assert reason is not None


async def test_poll_started_before_skip_and_resume_cannot_overwrite_restored_state(tmp_path):
    rig = await started(tmp_path)
    generations = []

    async def toggle(_):
        rig.provider.hook = None
        await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
        assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND) is None
        generations.append(rig.occ().generation)

    rig.provider.hook = toggle
    await rig.rt.poll_once()
    assert generations and rig.occ().generation == generations[0]
    assert not rig.occ().not_travelling


async def test_confirmed_arrival_and_physical_history_are_not_resumed(tmp_path):
    rig = await started(tmp_path)
    record = rig.rt.state.journey_records[OUT]
    arrived = replace(
        record, confirmations=(ArrivalConfirmation("arrived", record.destination, at("07:49")),)
    )
    seed_state(rig.rt, journey_records={OUT: arrived})
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    before = rig.occ()
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND) is None
    assert rig.occ() == before
    assert rig.rt.state.journey_records[OUT] == arrived


async def test_repeated_skip_keeps_original_state_for_resume(tmp_path):
    rig = await started(tmp_path)
    before = rig.occ()
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND)
    assert await rig.rt.travelling("p1", DAY, Direction.OUTBOUND) is None
    assert rig.occ().direction_state is before.direction_state


async def test_return_resume_after_midnight_targets_original_commute_day(tmp_path):
    rig = Rig(tmp_path)
    rig.write(
        text=PROFILE.replace(
            'return: {first_train: "17:52", buffer_min: 5}',
            'return: {first_train: "00:52", buffer_min: 5, day_offset: 1}',
        )
    )
    await rig.rt.start()
    await rig.rt.not_travelling("p1", DAY, Direction.OUTBOUND, whole_day=True)
    rig.clock.t = at("00:20", DAY + timedelta(days=1))
    assert (
        await rig.rt.travelling("p1", DAY + timedelta(days=1), Direction.RETURN, whole_day=True)
        is None
    )
    assert rig.occ(OUT).not_travelling
    assert not rig.occ("p1|return|2026-10-28").not_travelling


async def test_resume_uses_running_routes_day_offset_after_profile_edit(tmp_path):
    rig = Rig(tmp_path, "17:40")
    rig.provider.world = [make_connection([make_leg("RTD", "AAA", "17:52", "18:30")])]
    await rig.rt.start()
    await rig.rt.poll_once()
    before = rig.occ(BACK)
    assert before.lock is not None
    rig.write(
        text=PROFILE.replace(
            'return: {first_train: "17:52", buffer_min: 5}',
            'return: {first_train: "00:52", buffer_min: 5, day_offset: 1}',
        )
    )
    await rig.rt.reload()
    await rig.rt.not_travelling("p1", DAY, Direction.RETURN, whole_day=True)
    assert await rig.rt.travelling("p1", DAY, Direction.RETURN, whole_day=True) is None
    assert not rig.occ(BACK).not_travelling
    assert rig.occ(BACK).lock == before.lock

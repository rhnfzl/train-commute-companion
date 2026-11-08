import asyncio
from dataclasses import replace

import pytest
from builders import cut_stop
from vehicle_rig import make_rig

from train_commute_companion.model import EventKind
from train_commute_companion.providers.base import AuthError, ServerError


async def test_runtime_exposes_only_fresh_context_without_changing_the_train_lock(tmp_path):
    rig = make_rig(tmp_path)
    await rig.rt.start()
    locked = rig.occ().lock
    await rig.rt.vehicle_context_idle()
    context = rig.rt.view().occurrences[0].train_context
    assert context.context.train_type == "VIRM"
    assert context.context.station == "AAA"
    assert context.position.train_number == locked.legs[0].train.train_number
    assert not context.operating_date_confirmed and context.measurement_at is None
    assert rig.occ().lock == locked
    assert [call[0] for call in rig.provider.optional_calls] == ["positions", "context"]
    rig.clock.jump(2)
    expired = rig.rt.view().occurrences[0].train_context
    assert expired.context is None and expired.position is None


async def test_stopped_vehicle_refresh_owner_cannot_start_again(tmp_path):
    rig = make_rig(tmp_path)
    await rig.rt.start()
    await rig.rt.vehicle_context_idle()
    before = list(rig.provider.optional_calls)
    rig.clock.jump(2)
    await rig.rt.stop_vehicle_context()
    rig.rt._refresh_vehicles_soon()
    await rig.rt.vehicle_context_idle()
    assert rig.provider.optional_calls == before


async def test_slow_optional_request_never_delays_primary_warnings_and_is_cancelled_on_stop(
    tmp_path,
):
    rig = make_rig(tmp_path)
    rig.provider.positions_gate = asyncio.Event()
    await rig.rt.start()
    await asyncio.wait_for(rig.provider.positions_entered.wait(), 1)
    original = rig.provider.world[0]
    rig.clock.jump(2)
    rig.provider.world = [
        replace(
            original,
            legs=(
                replace(
                    original.legs[0],
                    cancelled=True,
                    stops=tuple(cut_stop(stop) for stop in original.legs[0].stops),
                ),
                *original.legs[1:],
            ),
        )
    ]
    try:
        await asyncio.wait_for(rig.rt.poll_once(), 1)
        assert any(event.kind is EventKind.CANCELLED for event in rig.notifier.attempts)
    finally:
        await asyncio.wait_for(rig.rt.stop_vehicle_context(), 1)
    assert rig.rt._vehicle_task.done()


@pytest.mark.parametrize(
    "error", [AuthError("optional entitlement"), ServerError("optional outage")]
)
async def test_optional_product_failure_does_not_reject_the_main_key_or_back_off_trips(
    tmp_path, error
):
    rig = make_rig(tmp_path)
    rig.provider.context_error = error
    await rig.rt.start()
    await rig.rt.vehicle_context_idle()
    assert not rig.rt.key_rejected and rig.rt._gate.budget.cooldown_until is None
    calls = len(rig.provider.calls)
    await rig.rt.poll_once()
    await rig.rt.vehicle_context_idle()
    assert len(rig.provider.calls) > calls

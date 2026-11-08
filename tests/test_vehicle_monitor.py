import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from train_commute_companion.budget import RequestBudget, RequestGate
from train_commute_companion.calendar import TZ
from train_commute_companion.model import TrainId
from train_commute_companion.providers.base import (
    AuthError,
    ProviderError,
    RateLimited,
    ServerError,
)
from train_commute_companion.vehicle import (
    FRESH_FOR,
    TrainContext,
    VehiclePosition,
    VehiclePositions,
    VehicleWatch,
)
from train_commute_companion.vehicle_monitor import VehicleMonitor

NOW = datetime(2026, 11, 6, 21, 52, tzinfo=TZ)
TRAIN = TrainId(NOW.date(), "579", NOW)
WATCH = VehicleWatch(TRAIN, "UT", NOW)


class Clock:
    at = NOW

    def now(self):
        return self.at


def context(train, station, at):
    return TrainContext(
        train, station, at, "DVS", "VIRM", "NS", "12", False, 6, 161, None, None, None
    )


def position(number):
    return VehiclePosition(number, number, 52.089, 5.110, "OBIS", "IC", 0, 149, 400)


class Provider:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.position_error = self.context_error = None
        self.position_result = self.context_result = None
        self.before_context = lambda: None
        self.positions_started = self.positions_release = None

    async def vehicle_positions(self):
        self.calls.append("positions")
        if self.positions_started is not None:
            self.positions_started.set()
            await self.positions_release.wait()
        if self.position_error is not None:
            raise self.position_error
        return self.position_result or VehiclePositions(
            self.clock.now(), (position("579"), position("1776"))
        )

    async def train_context(self, train, station):
        self.calls.append((train.train_number, station))
        self.before_context()
        if self.context_error is not None:
            raise self.context_error
        return self.context_result or context(train, station, self.clock.now())


def setup(callback=None):
    clock = Clock()
    provider = Provider(clock)
    gate = RequestGate(provider, clock.now)
    return clock, provider, gate, VehicleMonitor(provider, gate, clock.now, callback)


async def test_clock_rollback_does_not_hold_optional_context_in_future_cooldown():
    clock, provider, _gate, monitor = setup()
    await monitor.refresh((WATCH,))
    clock.at -= timedelta(minutes=1)
    current = replace(WATCH, journey_checked_at=clock.at)
    await monitor.refresh((current,))
    assert provider.calls.count(("579", "UT")) == 2
    assert monitor.snapshot(current, clock.now()).context.fetched_at == clock.now()


async def test_global_feed_is_shared_and_each_call_consumes_one_gate_request():
    clock, provider, gate, monitor = setup()
    other = replace(WATCH, train=replace(TRAIN, train_number="1776"))
    await monitor.refresh((WATCH, other, WATCH))
    assert gate.requests == 3 and provider.calls.count("positions") == 1
    assert monitor.snapshot(WATCH, clock.now()).position.train_number == "579"
    assert monitor.snapshot(other, clock.now()).position.train_number == "1776"
    await monitor.refresh((replace(WATCH, journey_checked_at=clock.now()), other))
    assert gate.requests == 3
    clock.at += FRESH_FOR
    await monitor.refresh((WATCH, other))
    assert gate.requests == 6
    snapshot = monitor.snapshot(WATCH, clock.now())
    assert snapshot.coverage == "unknown" and snapshot.measurement_at is None
    assert not snapshot.operating_date_confirmed and not snapshot.capacity_is_availability
    assert not snapshot.equipment_operation_confirmed


async def test_optional_requests_wait_behind_live_budget_cooldown():
    clock, provider, gate, monitor = setup()
    gate.budget = RequestBudget(clock.at + timedelta(minutes=1))
    await monitor.refresh((WATCH,))
    assert not provider.calls and gate.requests == 0
    clock.at += timedelta(minutes=1)
    assert gate.budget.allow(clock.at, live=True)
    await monitor.refresh((WATCH,))
    assert gate.requests == 0  # listing also waits the extra live-priority interval
    clock.at += timedelta(minutes=2)
    await monitor.refresh((WATCH,))
    assert gate.requests == 2


@pytest.mark.parametrize(
    "watch",
    [
        replace(WATCH, journey_checked_at=NOW - timedelta(minutes=5)),
        replace(WATCH, journey_checked_at=NOW + timedelta(seconds=1)),
        replace(WATCH, journey_checked_at=NOW.replace(tzinfo=None)),
        replace(WATCH, train=replace(TRAIN, operating_date=NOW.date() - timedelta(days=1))),
        replace(WATCH, train=replace(TRAIN, planned_departure=NOW + timedelta(days=1))),
        replace(WATCH, station="@place:bus"),
    ],
)
async def test_unsupported_watch_proof_never_requests_or_exposes_context(watch):
    clock, provider, gate, monitor = setup()
    await monitor.refresh((watch,))
    assert not provider.calls and gate.requests == 0
    snapshot = monitor.snapshot(watch, clock.now())
    assert snapshot.context is snapshot.position is None


@pytest.mark.parametrize(
    "failure", [AuthError("secret"), ServerError("secret"), ProviderError("secret")]
)
async def test_optional_context_errors_never_set_main_auth_or_server_backoff(failure):
    clock, provider, gate, monitor = setup()
    provider.context_error = failure
    await monitor.refresh((WATCH,))
    snapshot = monitor.snapshot(WATCH, clock.at)
    assert snapshot.context is None and snapshot.position is not None
    assert gate.budget.cooldown_until is None
    assert gate.requests == 2
    await monitor.refresh((WATCH,))
    assert gate.requests == 2


async def test_optional_feed_failure_does_not_prevent_train_context():
    clock, provider, gate, monitor = setup()
    provider.position_error = ServerError("optional server outage")
    await monitor.refresh((WATCH,))
    snapshot = monitor.snapshot(WATCH, clock.at)
    assert snapshot.context is not None and snapshot.position is None
    assert gate.requests == 2 and gate.budget.cooldown_until is None


async def test_rate_limit_is_shared_and_stops_later_optional_requests():
    clock, provider, gate, monitor = setup()
    provider.position_error = RateLimited("limited", 42)
    await monitor.refresh((WATCH,))
    assert provider.calls == ["positions"] and gate.requests == 1
    assert gate.budget.reason == "rate" and not gate.budget.allow(clock.at, live=True)


async def test_stale_results_are_hidden_and_failed_refresh_does_not_renew_them():
    clock, provider, gate, monitor = setup()
    await monitor.refresh((WATCH,))
    clock.at += FRESH_FOR
    provider.position_error = provider.context_error = ProviderError("offline")
    await monitor.refresh((WATCH,))
    snapshot = monitor.snapshot(WATCH, clock.at)
    assert snapshot.context is snapshot.position is None
    assert snapshot.context_checked_at == snapshot.positions_checked_at == NOW
    before = gate.requests
    await monitor.refresh((WATCH,))
    assert gate.requests == before


@pytest.mark.parametrize(
    "change",
    [
        {"requested_train": replace(TRAIN, train_number="999")},
        {"station": "ZL"},
        {"fetched_at": NOW + timedelta(seconds=1)},
        {"fetched_at": NOW - FRESH_FOR},
    ],
)
async def test_mismatched_or_not_current_context_cannot_attach_to_a_watch(change):
    clock, provider, _gate, monitor = setup()
    provider.context_result = replace(context(TRAIN, "UT", NOW), **change)
    await monitor.refresh((WATCH,))
    assert monitor.snapshot(WATCH, clock.at).context is None


async def test_unmatched_ambiguous_and_future_position_data_are_unknown():
    for positions in (
        VehiclePositions(NOW, (position("999"),)),
        VehiclePositions(NOW, (position("579"), position("579"))),
        VehiclePositions(NOW + timedelta(seconds=1), (position("579"),)),
    ):
        clock, provider, _gate, monitor = setup()
        provider.position_result = positions
        await monitor.refresh((WATCH,))
        assert monitor.snapshot(WATCH, clock.at).position is None


async def test_changed_watch_callback_discards_in_flight_context_and_cleans_cache():
    watches = [WATCH]
    clock, provider, _gate, monitor = setup(lambda: watches)
    other = replace(WATCH, train=replace(TRAIN, train_number="1776"))
    provider.before_context = lambda: watches.__setitem__(slice(None), [other])
    await monitor.refresh((WATCH,))
    assert monitor.snapshot(WATCH, clock.at).context is None
    assert monitor.snapshot(other, clock.at).context is None
    assert monitor.snapshot(other, clock.at).position is None
    provider.before_context = lambda: None
    await monitor.refresh((other,))
    assert monitor.snapshot(other, clock.at).context.requested_train == other.train
    assert len(monitor._contexts) == 1
    watches.clear()
    assert monitor.snapshot(other, clock.at).context is None
    assert not monitor._contexts and not monitor._positions.positions


async def test_concurrent_new_refresh_invalidates_old_results_without_duplicate_feed():
    clock, provider, _gate, monitor = setup()
    provider.positions_started, provider.positions_release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(monitor.refresh((WATCH,)))
    await provider.positions_started.wait()
    other = replace(WATCH, train=replace(TRAIN, train_number="1776"))
    second = asyncio.create_task(monitor.refresh((other,)))
    await asyncio.sleep(0)
    provider.positions_release.set()
    await asyncio.gather(first, second)
    assert provider.calls.count("positions") == 1
    assert monitor.snapshot(WATCH, clock.at).context is None
    assert monitor.snapshot(other, clock.at).context.requested_train == other.train
    assert monitor.snapshot(other, clock.at).position is None

"""Schedule optional train context behind journey requests without changing journey state."""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from enum import StrEnum

from train_commute_companion.budget import RequestGate
from train_commute_companion.calendar import TZ, to_utc
from train_commute_companion.health import FRESH_FOR as JOURNEY_FRESH_FOR
from train_commute_companion.model import TrainId
from train_commute_companion.providers.base import ProviderError, RateLimited
from train_commute_companion.vehicle import (
    FRESH_FOR,
    TrainContext,
    VehiclePositions,
    VehicleProvider,
    VehicleSnapshot,
    VehicleWatch,
    is_fresh,
)

Key = tuple[TrainId, str]


class CachePhase(StrEnum):
    DUE = "due"
    IN_FLIGHT = "in_flight"
    FRESH = "fresh"
    COOLDOWN = "cooldown"


def cache_phase(
    value: TrainContext | VehiclePositions | None,
    attempted: datetime | None,
    in_flight: bool,
    now: datetime,
) -> CachePhase:
    if in_flight:
        return CachePhase.IN_FLIGHT
    if value is not None and is_fresh(value, now):
        return CachePhase.FRESH
    if attempted is not None and timedelta() <= to_utc(now) - to_utc(attempted) < FRESH_FOR:
        return CachePhase.COOLDOWN
    return CachePhase.DUE


def _current(watch: VehicleWatch, now: datetime) -> bool:
    times = (now, watch.journey_checked_at, watch.train.planned_departure)
    if any(t.tzinfo is None or t.utcoffset() is None for t in times):
        return False
    return (
        timedelta() <= to_utc(now) - to_utc(watch.journey_checked_at) < JOURNEY_FRESH_FOR
        and watch.train.operating_date == now.astimezone(TZ).date()
        and watch.train.planned_departure.astimezone(TZ).date() == watch.train.operating_date
        and re.fullmatch(r"[0-9]{1,10}", watch.train.train_number) is not None
        and re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", watch.station) is not None
    )


async def _optional[T](ask: Callable[[], Awaitable[T]]) -> T:
    try:
        return await ask()
    except RateLimited:
        raise  # Only optional-product throttling affects the shared journey budget.
    except ProviderError:
        raise ProviderError("optional vehicle context is unavailable") from None


class VehicleMonitor:
    """One global feed per two minutes, with independent per-train context retrievals."""

    def __init__(
        self,
        provider: VehicleProvider,
        gate: RequestGate,
        now: Callable[[], datetime],
        current_watches: Callable[[], Sequence[VehicleWatch]] | None = None,
    ) -> None:
        self._provider, self._gate, self._now = provider, gate, now
        self._current_watches = current_watches
        self._active: dict[Key, VehicleWatch] = {}
        self._contexts: dict[Key, TrainContext] = {}
        self._context_attempts: dict[Key, datetime] = {}
        self._positions: VehiclePositions | None = None
        self._positions_attempt: datetime | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._context_in_flight: set[Key] = set()
        self._positions_in_flight = False

    def _sync(self, watches: Sequence[VehicleWatch]) -> None:
        self._active = {
            (watch.train, watch.station): watch for watch in watches if _current(watch, self._now())
        }
        self._contexts = {
            key: value for key, value in self._contexts.items() if key in self._active
        }
        self._context_attempts = {
            key: value for key, value in self._context_attempts.items() if key in self._active
        }
        if self._positions is not None:
            numbers = {train.train_number for train, _ in self._active}
            self._positions = replace(
                self._positions,
                positions=tuple(p for p in self._positions.positions if p.train_number in numbers),
            )

    def _latest(self, generation: int) -> dict[Key, VehicleWatch]:
        if generation != self._generation:
            return {}
        if self._current_watches is not None:
            self._sync(self._current_watches())
        else:
            self._sync(tuple(self._active.values()))
        return self._active

    async def refresh(self, watches: Sequence[VehicleWatch]) -> None:
        """Call after primary polling. A callback detects watch changes during provider waits."""
        self._generation += 1
        generation = self._generation
        self._sync(self._current_watches() if self._current_watches is not None else watches)
        async with self._lock:
            active = dict(self._latest(generation))
            if not active or not self._gate.budget.allow(self._now(), live=False):
                return
            if (
                cache_phase(
                    self._positions, self._positions_attempt, self._positions_in_flight, self._now()
                )
                is CachePhase.DUE
            ):
                self._positions_attempt = self._now()
                self._positions_in_flight = True
                try:
                    result = await self._gate.listing(
                        lambda: _optional(self._provider.vehicle_positions), "vehicle positions"
                    )
                finally:
                    self._positions_in_flight = False
                latest = self._latest(generation)
                if (
                    isinstance(result, VehiclePositions)
                    and is_fresh(result, self._now())
                    and latest
                ):
                    numbers = {key[0].train_number for key in active.keys() & latest.keys()}
                    counts = {
                        number: sum(p.train_number == number for p in result.positions)
                        for number in numbers
                    }
                    self._positions = replace(
                        result,
                        positions=tuple(
                            replace(p, measurement_at=None, operating_date_confirmed=False)
                            for p in result.positions
                            if p.train_number in numbers
                            and counts[p.train_number] == 1
                            and not isinstance(p.latitude, bool)
                            and not isinstance(p.longitude, bool)
                            and math.isfinite(p.latitude)
                            and math.isfinite(p.longitude)
                            and -90 <= p.latitude <= 90
                            and -180 <= p.longitude <= 180
                        ),
                    )
            for key, watch in active.items():
                if key not in self._latest(generation):
                    continue
                if not self._gate.budget.allow(self._now(), live=False):
                    break
                cached = self._contexts.get(key)
                if (
                    cache_phase(
                        cached,
                        self._context_attempts.get(key),
                        key in self._context_in_flight,
                        self._now(),
                    )
                    is not CachePhase.DUE
                ):
                    continue
                self._context_attempts[key] = self._now()

                async def request_context(w: VehicleWatch = watch) -> TrainContext:
                    return await _optional(lambda: self._provider.train_context(w.train, w.station))

                self._context_in_flight.add(key)
                try:
                    context_result = await self._gate.listing(request_context, "train context")
                finally:
                    self._context_in_flight.discard(key)
                if key not in self._latest(generation):
                    continue
                if (
                    isinstance(context_result, TrainContext)
                    and context_result.requested_train == watch.train
                    and context_result.station == watch.station
                    and is_fresh(context_result, self._now())
                ):
                    self._contexts[key] = replace(
                        context_result, measurement_at=None, operating_date_confirmed=False
                    )

    def snapshot(self, watch: VehicleWatch, now: datetime) -> VehicleSnapshot:
        """Stale retrievals and unsupported identities produce unknown context, never stale markers."""
        active = self._latest(self._generation)
        key = (watch.train, watch.station)
        if key not in active or not _current(watch, now):
            return VehicleSnapshot(watch, None, None, None, None)
        context = self._contexts.get(key)
        positions = self._positions
        position = None
        if positions is not None and is_fresh(positions, now):
            position = next(
                (p for p in positions.positions if p.train_number == watch.train.train_number), None
            )
        return VehicleSnapshot(
            watch,
            context if context is not None and is_fresh(context, now) else None,
            position,
            None if context is None else context.fetched_at,
            None if positions is None else positions.fetched_at,
        )

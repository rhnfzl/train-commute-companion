"""Location fixes from Telegram or OwnTracks into core zone events (7.2, 8). Thin: the zone
consequences live in the core; here only which zone a fix is in and what changed."""

import asyncio
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from train_commute_companion.calendar import TZ
from train_commute_companion.location import (
    PLACE_ROLES,
    STALE_LOCATION,
    LocationEvent,
    Zone,
    ZoneTrack,
    next_zone,
    resolve_zone,
    seed_zone,
    station_zones,
    undelivered,
)
from train_commute_companion.model import LocationSettings
from train_commute_companion.runtime import Runtime
from train_commute_companion.view import running_stations

log = logging.getLogger(__name__)

FUTURE_LIMIT = timedelta(minutes=2)  # a phone clock ahead: a future fix would block later ones

Source = Literal["telegram", "owntracks"]


def stamp(v: object) -> datetime | None:
    """Unix seconds (an int, not a bool) as a `TZ` time; None when not one or out of range."""
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    try:
        return datetime.fromtimestamp(v, TZ)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Fix:
    lat: float
    lon: float
    at: datetime
    acc: float | None = None
    regions: frozenset[str] = frozenset()


@dataclass
class _Memory:
    """One profile's last fix under one source epoch; times in UTC."""

    epoch: int
    last_at: datetime
    track: ZoneTrack


def _zone_of(fix: Fix, loc: LocationSettings, zones: Sequence[Zone]) -> str | None:
    zone = resolve_zone(fix.lat, fix.lon, zones)
    if zone is not None:
        return zone
    # OwnTracks regions, by the configured name the core compares
    names = (loc.zones.get(role, role) for role in PLACE_ROLES)
    return next((n for n in names if n in fix.regions), None)


class Follower:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._locks: dict[str, asyncio.Lock] = {}  # a lock queues FIFO: arrival order per profile
        self._memory: dict[str, _Memory] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def feed(self, source: Source, fix: Fix) -> None:
        view = self._rt.view()
        for p in view.profiles:
            if p.location is not None and p.location.source == source:
                epoch = view.location_epochs.get(p.id, 0)
                self._spawn(self._handle(p.id, source, epoch, fix))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle(self, pid: str, source: Source, fed_epoch: int, fix: Fix) -> None:
        async with self._locks.setdefault(pid, asyncio.Lock()):
            try:
                await self._step(pid, source, fed_epoch, fix)
            except Exception:
                log.exception("location fix for %s failed", pid)

    async def _step(self, pid: str, source: Source, fed_epoch: int, fix: Fix) -> None:
        view = self._rt.view()
        profile = next((p for p in view.profiles if p.id == pid), None)
        loc = profile.location if profile is not None else None
        epoch = view.location_epochs.get(pid, 0)
        # a fix queued behind a slow core call while the source went A, B, A: dropped, no seed
        if profile is None or loc is None or loc.source != source or epoch != fed_epoch:
            return
        now, at = self._rt.now().astimezone(UTC), fix.at.astimezone(UTC)
        if at > now + FUTURE_LIMIT or now - at > STALE_LOCATION:
            return
        if fix.acc is not None and fix.acc > loc.station_radius_m:
            return
        running = running_stations(view.occurrences, pid)
        zones = station_zones(profile, loc.station_radius_m, self._rt.positions, running)
        zone = _zone_of(fix, loc, zones)
        mem = self._memory.get(pid)
        if mem is None or mem.epoch != epoch:  # a new source: the first fix only seeds (7.2)
            self._memory[pid] = _Memory(epoch, at, seed_zone(zone, at))
            return
        if at <= mem.last_at:
            return
        mem.last_at = at
        mem.track, events = next_zone(mem.track, zone, at)
        try:
            for name, kind in events:
                event = LocationEvent(name, kind, fix.at)
                if not await self._rt.location(pid, event, loc, epoch):
                    mem.track = undelivered(mem.track)
        except Exception:
            mem.track = undelivered(mem.track)
            raise

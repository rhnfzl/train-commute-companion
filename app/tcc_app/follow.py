"""Location fixes from Telegram or OwnTracks into core zone events (7.2, 8). Thin: the zone
consequences live in the core; here only which zone a fix is in and what changed."""

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from train_commute_companion.calendar import TZ
from train_commute_companion.lifecycle import STALE_LOCATION
from train_commute_companion.location import LocationEvent, Zone, resolve_zone
from train_commute_companion.model import LocationSettings, Profile
from train_commute_companion.runtime import Runtime
from train_commute_companion.view import RuntimeView

log = logging.getLogger(__name__)

REPORT_EVERY = timedelta(minutes=2)  # each event costs a live request per occurrence
FUTURE_LIMIT = timedelta(minutes=2)  # a phone clock ahead: a future fix would block later ones
PLACE_ROLES = ("home", "work")

Source = Literal["telegram", "owntracks"]


def number(v: object) -> float | None:
    """A finite JSON number (not a bool), else None."""
    ok = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None  # type: ignore[arg-type]


def stamp(v: object) -> datetime | None:
    """Unix seconds (an int, not a bool) as a `TZ` time; None when not one or out of range."""
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    try:
        return datetime.fromtimestamp(v, TZ)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
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
    prev: str | None
    last_at: datetime
    reported: dict[str, datetime] = field(default_factory=dict)


def _zones(
    view: RuntimeView,
    p: Profile,
    loc: LocationSettings,
    positions: Mapping[str, tuple[float, float]],
) -> list[Zone]:
    """Station zones of the route and of every running or retained leg (an old route keeps its)."""
    legs = [
        leg
        for o in view.occurrences
        if o.key.profile_id == p.id
        for leg in (*o.legs, *(x for r in o.retained for x in r))
    ]
    codes = {
        p.home_station,
        p.work_station,
        *p.outbound.transfers,
        *p.return_.transfers,
        *(s for leg in legs for s in (leg.board, leg.alight)),
    }
    radius = float(loc.station_radius_m)
    return [Zone(c, *positions[c], radius) for c in sorted(codes) if c in positions]


class Follower:
    def __init__(
        self, rt: Runtime, positions: Callable[[], Mapping[str, tuple[float, float]]]
    ) -> None:
        self._rt, self._positions = rt, positions
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
        zone = resolve_zone(fix.lat, fix.lon, _zones(view, profile, loc, self._positions()))
        if zone is None:  # OwnTracks regions, by the configured name the core compares
            names = (loc.zones.get(role, role) for role in PLACE_ROLES)
            zone = next((n for n in names if n in fix.regions), None)
        mem = self._memory.get(pid)
        if mem is None or mem.epoch != epoch:  # a new source: the first fix only seeds (7.2)
            self._memory[pid] = _Memory(epoch, zone, at, {zone: at} if zone else {})
            return
        if at <= mem.last_at:
            return
        prev, mem.prev, mem.last_at = mem.prev, zone, at
        events: list[tuple[str, Literal["enter", "leave", "report"]]] = []
        if zone != prev:
            if prev is not None:
                events.append((prev, "leave"))
            if zone is not None:
                events.append((zone, "enter"))
                mem.reported[zone] = at  # an enter starts the report timer
        elif zone is not None:
            last = mem.reported.get(zone)
            if last is None or at - last >= REPORT_EVERY:
                events.append((zone, "report"))
                mem.reported[zone] = at
        for name, kind in events:
            event = LocationEvent(name, kind, fix.at, self._rt.now())
            await self._rt.location(pid, event, loc, epoch)

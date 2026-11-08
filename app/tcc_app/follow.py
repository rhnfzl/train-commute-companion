"""Location fixes from Telegram or OwnTracks into core zone events (7.2, 8). Thin: the zone step
and its consequences live in the core; here only whether a fix is fit to read and its memory."""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from train_commute_companion.calendar import TZ
from train_commute_companion.location import (
    STALE_LOCATION,
    Fix,
    ZoneTrack,
    send_zone_events,
    without_current_report,
)
from train_commute_companion.runtime import Runtime
from train_commute_companion.view import track_fix

log = logging.getLogger(__name__)

FUTURE_LIMIT = timedelta(minutes=2)  # a phone clock ahead: a future fix would block later ones

Source = Literal["telegram", "owntracks"]


class FollowPhase(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


def stamp(v: object) -> datetime | None:
    """Unix seconds (an int, not a bool) as a `TZ` time; None when not one or out of range."""
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    try:
        return datetime.fromtimestamp(v, TZ)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(slots=True)
class _Memory:
    """One profile's last fix under one source epoch; `last_at` in UTC."""

    epoch: int
    last_at: datetime
    track: ZoneTrack


class Follower:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._locks: dict[str, asyncio.Lock] = {}  # a lock queues FIFO: arrival order per profile
        self._memory: dict[str, _Memory] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.phase = FollowPhase.ACTIVE

    def feed(self, source: Source, fix: Fix) -> None:
        if self.phase is not FollowPhase.ACTIVE:
            return
        view = self._rt.view()
        for p in view.profiles:
            if p.location is not None and p.location.source == source:
                epoch = view.location_epochs.get(p.id, 0)
                self._spawn(self._process_fix(p.id, source, epoch, fix))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        self.phase = FollowPhase.CLOSING
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.phase = FollowPhase.CLOSED

    async def _process_fix(self, pid: str, source: Source, fed_epoch: int, fix: Fix) -> None:
        async with self._locks.setdefault(pid, asyncio.Lock()):
            try:
                await self._apply_fix(pid, source, fed_epoch, fix)
            except Exception:
                log.exception("location fix for %s failed", pid)

    async def _apply_fix(self, pid: str, source: Source, fed_epoch: int, fix: Fix) -> None:
        if self.phase is not FollowPhase.ACTIVE:
            return
        view = self._rt.view()
        profile = next((p for p in view.profiles if p.id == pid), None)
        loc = profile.location if profile is not None else None
        if profile is None or loc is None or loc.source != source:
            return
        now, at = self._rt.now().astimezone(UTC), fix.at.astimezone(UTC)
        if at > now + FUTURE_LIMIT or now - at > STALE_LOCATION:
            return
        known = self._memory.get(pid)
        if known is not None and known.epoch != fed_epoch:  # a new source: its first fix seeds
            known = None
        if known is not None and at <= known.last_at:
            return
        # a fix queued behind a slow core call while the source went A, B, A: dropped, no seed
        step = track_fix(view, profile, fed_epoch, fix, known.track if known else None)
        if step is None:
            return
        track, events = step
        mem = self._memory[pid] = _Memory(fed_epoch, at, track)

        def undelivered() -> None:
            mem.track = without_current_report(mem.track)

        await send_zone_events(
            events, fix.at, lambda e: self._rt.report_location(pid, e, loc, fed_epoch), undelivered
        )

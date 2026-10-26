"""Location source (7.2, 8): a person or device tracker in, core location events out. Location
only adds alerts; the zone logic and every consequence of an event live in the core."""

import asyncio
import logging
import math
from collections.abc import Coroutine, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from train_commute_companion.location import LocationEvent, Zone, resolve_zone
from train_commute_companion.model import Profile
from train_commute_companion.runtime import Runtime

if TYPE_CHECKING:  # data.py imports this module
    from .data import TccConfigEntry  # noqa: TC004 (annotation only, lazy on 3.14)

_LOGGER = logging.getLogger(__name__)

REPORT_EVERY = timedelta(minutes=2)  # HA-46: each event costs a live request per occurrence
PLACE_ROLES = ("home", "work")


def _num(v: object) -> float | None:
    ok = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None  # type: ignore[arg-type]


def _fix(state: State | None) -> tuple[float, float] | None:
    """The coordinates of a state; None for unavailable, unknown or a tracker with no GPS."""
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    lat, lon = _num(state.attributes.get("latitude")), _num(state.attributes.get("longitude"))
    return None if lat is None or lon is None else (lat, lon)


def zones_for(
    hass: HomeAssistant,
    profile: Profile,
    positions: Mapping[str, tuple[float, float]],
    running: Iterable[str] = (),
) -> list[Zone]:
    loc = profile.location
    if loc is None:
        return []
    codes = {
        profile.home_station,
        profile.work_station,
        *profile.outbound.transfers,
        *profile.return_.transfers,
        *running,
    }
    zones = [
        Zone(c, *positions[c], float(loc.station_radius_m)) for c in sorted(codes) if c in positions
    ]
    for role in PLACE_ROLES:
        entity = loc.zones.get(role) or ("zone.home" if role == "home" else None)
        state = None if entity is None else hass.states.get(entity)
        if state is None:
            continue
        lat, lon = _fix(state) or (None, None)
        radius = _num(state.attributes.get("radius"))
        if lat is not None and lon is not None and radius is not None:
            zones.append(Zone(loc.zones.get(role, role), lat, lon, radius))
    return zones


class LocationTracker:
    def __init__(self, hass: HomeAssistant, entry: TccConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._tracked: dict[str, str] = {}  # profile id -> entity
        self._unsub_state: Any = None
        self._unsub_data: Any = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[str, asyncio.Lock] = {}  # a lock queues FIFO: arrival order per profile
        self._prev: dict[str, str | None] = {}  # profile id -> last zone
        self._reported: dict[tuple[str, str], datetime] = {}

    def start(self) -> None:
        self._unsub_data = self._entry.runtime_data.coordinator.async_add_listener(self._sync)
        self._sync()

    async def stop(self) -> None:
        for unsub in (self._unsub_state, self._unsub_data):
            if unsub is not None:
                unsub()
        self._unsub_state = self._unsub_data = None
        self._tracked = {}
        self._prev.clear()
        self._reported.clear()
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @callback
    def _sync(self) -> None:
        """Follows the profiles of the coordinator's view, removed ones that still run included."""
        data = self._entry.runtime_data.coordinator.data
        want = {
            p.id: p.location.entity
            for p in (data.profiles if data else ())
            if p.location and p.location.source == "ha" and p.location.entity
        }
        old, self._tracked = self._tracked, want
        if self._unsub_state is None or set(want.values()) != set(old.values()):
            if self._unsub_state is not None:
                self._unsub_state()
            entities = sorted(set(want.values()))
            self._unsub_state = (
                async_track_state_change_event(self._hass, entities, self._on_change)
                if entities
                else None
            )
        for pid in old.keys() - want.keys():
            self._prev.pop(pid, None)
            self._reported = {k: v for k, v in self._reported.items() if k[0] != pid}
        for pid, entity in want.items():
            if old.get(pid) != entity:  # 7.2: the state found now only seeds the zone
                state = self._hass.states.get(entity)
                if (pos := _fix(state)) is not None and state is not None:
                    self._spawn(self._handle(pid, pos, state.last_updated, seed=True), pid)

    @callback
    def _on_change(self, event: Event[EventStateChangedData]) -> None:
        new, old = event.data["new_state"], event.data["old_state"]
        pos = _fix(new)
        # an unchanged fix is no news: last_updated also moves on an attribute change (rule 7)
        if new is None or pos is None or (old is not None and _fix(old) == pos):
            return
        for pid, entity in self._tracked.items():
            if entity == event.data["entity_id"]:
                self._spawn(self._handle(pid, pos, new.last_updated), pid)

    def _spawn(self, coro: Coroutine[Any, Any, None], pid: str) -> None:
        task = self._entry.async_create_task(self._hass, coro, f"tcc location {pid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _running(self, pid: str) -> set[str]:
        """Stations of every lock and retained leg: an old route or reroute keeps its zones."""
        data = self._entry.runtime_data.coordinator.data
        legs = [
            leg
            for o in data.occurrences
            if o.key.profile_id == pid
            for leg in (*o.legs, *(x for r in o.retained for x in r))
        ]
        return {s for leg in legs for s in (leg.board, leg.alight)}

    async def _handle(
        self, pid: str, pos: tuple[float, float], at: datetime, *, seed: bool = False
    ) -> None:
        host = self._entry.runtime_data.host
        async with self._locks.setdefault(pid, asyncio.Lock()):
            try:
                profile = next(
                    (p for p in self._entry.runtime_data.coordinator.data.profiles if p.id == pid),
                    None,
                )
                if profile is None or profile.location is None:
                    return
                zones = zones_for(self._hass, profile, await host.positions(), self._running(pid))
                zone = resolve_zone(*pos, zones)
                prev, self._prev[pid] = self._prev.get(pid), zone
                if seed:
                    return
                events: list[LocationEvent] = []
                if zone != prev:
                    if prev is not None:
                        events.append(LocationEvent(prev, "leave", at, at))
                    if zone is not None:
                        events.append(LocationEvent(zone, "enter", at, at))
                elif zone is not None:
                    last = self._reported.get((pid, zone))
                    if last is None or at - last >= REPORT_EVERY:
                        self._reported[(pid, zone)] = at
                        events.append(LocationEvent(zone, "report", at, at))
                for event in events:

                    def send(rt: Runtime, msg: LocationEvent = event) -> Coroutine[Any, Any, None]:
                        return rt.location(pid, replace(msg, received_at=rt.now()))

                    await host.run(send)
            except RuntimeError:
                _LOGGER.debug("location event dropped: the runtime is not running")
            except Exception:
                _LOGGER.exception("location event for %s failed", pid)

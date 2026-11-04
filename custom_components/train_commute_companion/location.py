"""Location source (7.2, 8): a person or device tracker in, core location events out. Location
only adds alerts; the zone logic and every consequence of an event live in the core."""

import asyncio
import logging
from collections.abc import Coroutine, Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from train_commute_companion.location import (
    PLACE_ROLES,
    LocationEvent,
    Zone,
    ZoneTrack,
    next_zone,
    number,
    resolve_zone,
    seed_zone,
    station_zones,
    undelivered,
)
from train_commute_companion.model import Profile
from train_commute_companion.runtime import Runtime
from train_commute_companion.view import running_stations

from .host import RuntimeNotRunning

if TYPE_CHECKING:  # data.py imports this module
    from .data import TccConfigEntry  # noqa: TC004 (annotation only, lazy on 3.14)

_LOGGER = logging.getLogger(__name__)


def _fix(state: State) -> tuple[float, float] | None:
    """The coordinates of a state; None for unavailable, unknown or a tracker with no GPS."""
    if state.state in ("unavailable", "unknown"):
        return None
    lat, lon = number(state.attributes.get("latitude")), number(state.attributes.get("longitude"))
    return None if lat is None or lon is None else (lat, lon)


def _acc(state: State) -> float | None:
    return number(state.attributes.get("gps_accuracy"))


def zones_for(
    hass: HomeAssistant,
    profile: Profile,
    positions: Mapping[str, tuple[float, float]],
    running: Iterable[str] = (),
) -> list[Zone]:
    loc = profile.location
    if loc is None:
        return []
    zones = station_zones(profile, loc.station_radius_m, positions, running)
    for role in PLACE_ROLES:
        entity = loc.zones.get(role) or ("zone.home" if role == "home" else None)
        state = None if entity is None else hass.states.get(entity)
        if state is None:
            continue
        lat, lon = _fix(state) or (None, None)
        radius = number(state.attributes.get("radius"))
        if lat is not None and lon is not None and radius is not None:
            zones.append(Zone(loc.zones.get(role, role), lat, lon, radius))
    return zones


class LocationTracker:
    def __init__(self, hass: HomeAssistant, entry: TccConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        # profile id -> (entity, source epoch): the entity lower case like HA's ids, the epoch
        # from the same coordinator view; a change of either is a new source
        self._tracked: dict[str, tuple[str, int]] = {}
        # profile id -> token of its current source; work queued under an older token is dropped
        self._gen: dict[str, object] = {}
        self._unsub_state: Any = None
        self._unsub_data: Any = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[str, asyncio.Lock] = {}  # a lock queues FIFO: arrival order per profile
        self._tracks: dict[str, ZoneTrack] = {}

    def start(self) -> None:
        self._unsub_data = self._entry.runtime_data.coordinator.async_add_listener(self._sync)
        self._sync()

    async def stop(self) -> None:
        for unsub in (self._unsub_state, self._unsub_data):
            if unsub is not None:
                unsub()
        self._unsub_state = self._unsub_data = None
        self._tracked = {}
        self._gen.clear()
        self._tracks.clear()
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @callback
    def _sync(self) -> None:
        """Follows the profiles of the coordinator's view, removed ones that still run included."""
        data = self._entry.runtime_data.coordinator.data
        want = {
            p.id: (p.location.entity.lower(), data.location_epochs.get(p.id, 0))
            for p in (data.profiles if data else ())
            if p.location and p.location.source == "ha" and p.location.entity
        }
        old, self._tracked = self._tracked, want
        entities = sorted({e for e, _ in want.values()})
        if self._unsub_state is None or entities != sorted({e for e, _ in old.values()}):
            if self._unsub_state is not None:
                self._unsub_state()
            self._unsub_state = (
                async_track_state_change_event(self._hass, entities, self._on_change)
                if entities
                else None
            )
        for pid in old.keys() | want.keys():
            tracked = want.get(pid)
            if old.get(pid) == tracked:
                continue
            # a new source starts with no history: an old zone or pending event is not its evidence
            self._tracks.pop(pid, None)
            if tracked is None:
                self._gen.pop(pid, None)
                continue
            entity, epoch = tracked
            self._gen[pid] = gen = object()
            state = self._hass.states.get(entity)  # 7.2: the state found now only seeds the zone
            if state is not None and (pos := _fix(state)) is not None:
                at, acc = state.last_updated, _acc(state)
                self._spawn(self._handle(pid, gen, epoch, pos, at, acc, seed=True), pid)

    @callback
    def _on_change(self, event: Event[EventStateChangedData]) -> None:
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or (pos := _fix(new)) is None:
            return
        # an unchanged fix is no news: last_updated also moves on an attribute change (rule 7)
        if old is not None and _fix(old) == pos and _acc(old) == _acc(new):
            return
        for pid, (entity, epoch) in self._tracked.items():
            if entity == event.data["entity_id"]:
                at = new.last_updated
                self._spawn(self._handle(pid, self._gen[pid], epoch, pos, at, _acc(new)), pid)

    def _spawn(self, coro: Coroutine[Any, Any, None], pid: str) -> None:
        task = self._entry.async_create_task(self._hass, coro, f"tcc location {pid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(
        self,
        pid: str,
        gen: object,
        epoch: int,
        pos: tuple[float, float],
        at: datetime,
        acc: float | None,
        *,
        seed: bool = False,
    ) -> None:
        """`gen` is the source token at the change: a switch, disable or new entity since then
        drops the work, so evidence of an old source never reaches the core. `epoch` is the
        core's location epoch when the fix arrived: work queued behind the lock while the source
        changed (a switch back included) is dropped here and again by the core."""
        host = self._entry.runtime_data.host

        def live() -> bool:
            return self._gen.get(pid) is gen

        async with self._locks.setdefault(pid, asyncio.Lock()):
            try:
                data = self._entry.runtime_data.coordinator.data
                profile = next((p for p in data.profiles if p.id == pid), None)
                if profile is None or profile.location is None:
                    return
                if data.location_epochs.get(pid, 0) != epoch:  # a lagging view drops it too
                    return
                source = profile.location
                if acc is not None and acc > source.station_radius_m:
                    return  # it cannot place the user in a station circle (8, rule 6)
                positions = await host.run(lambda rt: dict(rt.positions))
                if not live():
                    return
                # read again after the await: the view may have moved on
                running = running_stations(
                    self._entry.runtime_data.coordinator.data.occurrences, pid
                )
                zone = resolve_zone(*pos, zones_for(self._hass, profile, positions, running))
                if seed:
                    self._tracks[pid] = seed_zone(zone, at)
                    return
                track = self._tracks.get(pid, ZoneTrack())  # never seeded: no zone to leave
                self._tracks[pid], changes = next_zone(track, zone, at)
                try:
                    for name, kind in changes:
                        if not live():  # checked with no await before the dispatch
                            return
                        event = LocationEvent(name, kind, at)

                        def send(
                            rt: Runtime, msg: LocationEvent = event
                        ) -> Coroutine[Any, Any, None]:
                            # the core rechecks the source after its own request, before any write
                            return rt.location(pid, msg, source, epoch=epoch)

                        await host.run(send)
                except Exception:
                    if pid in self._tracks:  # a source change meanwhile has dropped this track
                        self._tracks[pid] = undelivered(self._tracks[pid])
                    raise
            except RuntimeNotRunning:
                _LOGGER.debug("location event dropped: the runtime is not running")
            except Exception:
                _LOGGER.exception("location event for %s failed", pid)

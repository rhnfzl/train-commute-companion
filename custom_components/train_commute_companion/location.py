"""Location source (7.2, 8): a person or device tracker in, core location events out. Location
only adds alerts; the zone logic and every consequence of an event live in the core."""

import asyncio
import logging
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

from train_commute_companion.location import (
    PLACE_ROLES,
    Fix,
    LocationEvent,
    Zone,
    ZoneTrack,
    number,
    send_zone_events,
    without_current_report,
)
from train_commute_companion.model import LocationSettings
from train_commute_companion.runtime import Runtime
from train_commute_companion.view import track_fix

from .coordinator import TccCoordinator
from .host import RuntimeHost, RuntimeNotRunning

_LOGGER = logging.getLogger(__name__)


def _fix(state: State) -> tuple[float, float] | None:
    """The coordinates of a state; None for unavailable, unknown or a tracker with no GPS, and
    for a malformed accuracy: that is not "accuracy unknown" (a null is), the fix is not trusted."""
    if state.state in ("unavailable", "unknown"):
        return None
    if (raw := state.attributes.get("gps_accuracy")) is not None and number(raw) is None:
        return None
    lat, lon = number(state.attributes.get("latitude")), number(state.attributes.get("longitude"))
    return None if lat is None or lon is None else (lat, lon)


def _acc(state: State) -> float | None:
    return number(state.attributes.get("gps_accuracy"))


def place_zones(hass: HomeAssistant, loc: LocationSettings) -> list[Zone]:
    zones: list[Zone] = []
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
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: TccCoordinator,
        host: RuntimeHost,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._host = host
        # profile id -> (entity, source epoch): the entity lower case like HA's ids, the epoch
        # from the same coordinator view; a change of either is a new source
        self._tracked: dict[str, tuple[str, int]] = {}
        # profile id -> token of its current source; a send under an older token is dropped
        self._gen: dict[str, object] = {}
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_data: CALLBACK_TYPE | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[str, asyncio.Lock] = {}  # a lock queues FIFO: arrival order per profile
        self._tracks: dict[str, ZoneTrack] = {}

    def start(self) -> None:
        self._unsub_data = self._coordinator.async_add_listener(self._sync)
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
        data = self._coordinator.data
        want = {
            p.id: (p.location.entity.lower(), data.location_epochs.get(p.id, 0))
            for p in data.profiles
            if p.location and p.location.source == "ha" and p.location.entity
        }
        old, self._tracked = self._tracked, want
        self._subscribe(sorted({e for e, _ in want.values()}), sorted({e for e, _ in old.values()}))
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
                self._spawn(self._process_fix(pid, gen, epoch, pos, at, acc, seed=True), pid)

    def _subscribe(self, entities: list[str], before: list[str]) -> None:
        if self._unsub_state is not None and entities == before:
            return
        if self._unsub_state is not None:
            self._unsub_state()
        self._unsub_state = (
            async_track_state_change_event(self._hass, entities, self._on_change)
            if entities
            else None
        )

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
                self._spawn(self._process_fix(pid, self._gen[pid], epoch, pos, at, _acc(new)), pid)

    def _spawn(self, coro: Coroutine[Any, Any, None], pid: str) -> None:
        task = self._entry.async_create_task(self._hass, coro, f"tcc location {pid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_fix(
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
        """`epoch` is the core's location epoch when the fix arrived: work queued behind the lock
        while the source changed (a switch back included) is dropped here and again by the core.
        A new token always comes with a new epoch, so only the sends of a fix, which outlast the
        check, read `gen`: the rest of a fix's events are not dispatched for an old source."""
        async with self._locks.setdefault(pid, asyncio.Lock()):
            try:
                await self._apply_fix(pid, gen, epoch, pos, at, acc, seed)
            except RuntimeNotRunning:
                _LOGGER.debug("location event dropped: the runtime is not running")
            except Exception:
                _LOGGER.exception("location event for %s failed", pid)

    async def _apply_fix(
        self,
        pid: str,
        gen: object,
        epoch: int,
        pos: tuple[float, float],
        at: datetime,
        acc: float | None,
        seed: bool,
    ) -> None:
        data = self._coordinator.data
        profile = next((p for p in data.profiles if p.id == pid), None)
        if profile is None or profile.location is None:
            return
        source = profile.location
        # never seeded: no zone to leave
        track = None if seed else self._tracks.get(pid, ZoneTrack())
        fix = Fix(*pos, at, acc)
        step = track_fix(data, profile, epoch, fix, track, place_zones(self._hass, source))
        if step is None:  # a lagging view drops it too
            return
        self._tracks[pid], changes = step
        await send_zone_events(
            changes,
            at,
            lambda e: self._send(pid, gen, e, source, epoch),
            lambda: self._lift_timer(pid),
        )

    async def _send(
        self, pid: str, gen: object, event: LocationEvent, source: LocationSettings, epoch: int
    ) -> bool:
        if self._gen.get(pid) is not gen:  # checked with no await before the dispatch
            return True  # an old source is owed nothing, so its timer stays as it is

        def send(rt: Runtime) -> Coroutine[Any, Any, bool]:
            # the core rechecks the source after its own request, before any write
            return rt.report_location(pid, event, source, epoch=epoch)

        return await self._host.run(send)

    def _lift_timer(self, pid: str) -> None:
        if pid in self._tracks:  # a source change meanwhile has dropped this track
            self._tracks[pid] = without_current_report(self._tracks[pid])

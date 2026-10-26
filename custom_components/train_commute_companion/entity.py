"""Shared entity base, the occurrence picker, the health state and the entity lifecycle."""

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from train_commute_companion.health import FAILED_POLLS
from train_commute_companion.model import Direction
from train_commute_companion.view import OccurrenceView, RuntimeView

from .const import ATTRIBUTION, DOMAIN
from .coordinator import TccCoordinator


def unique_id(
    profile_id: str, key: str, direction: Direction | None = None, n: int | None = None
) -> str:
    """From the immutable profile id; the index is part of the key (`leg_2`)."""
    full = key if n is None else f"{key}_{n}"
    return f"{profile_id}_{full}" if direction is None else f"{profile_id}_{direction.value}_{full}"


class TccEntity(CoordinatorEntity[TccCoordinator]):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: TccCoordinator,
        profile_id: str,
        profile_name: str,
        key: str,
        direction: Direction | None = None,
        n: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.profile_id = profile_id
        self.direction = direction
        self.n = n
        self._attr_unique_id = unique_id(profile_id, key, direction, n)
        # one translation key for all legs (or transfers)
        self._attr_translation_key = key
        self._attr_translation_placeholders = {
            "direction": "" if direction is None else direction.value.capitalize(),
            "n": "" if n is None else str(n),
        }
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, profile_id)},
            name=profile_name,
            entry_type=DeviceEntryType.SERVICE,
        )


def pick(
    view: RuntimeView, profile_id: str, direction: Direction
) -> tuple[OccurrenceView | None, tuple[OccurrenceView, ...]]:
    """The oldest occurrence not resolved, else the newest, and the rest (7.13)."""
    mine = sorted(
        (
            o
            for o in view.occurrences
            if o.key.profile_id == profile_id and o.key.direction is direction
        ),
        key=lambda o: o.key.service_date,
    )
    if not mine:
        return None, ()
    chosen = next((o for o in mine if not o.resolved), mine[-1])
    return chosen, tuple(o for o in mine if o is not chosen)


def health_state(view: RuntimeView, profile_id: str) -> str:
    if view.key_rejected:
        return "key_rejected"
    if not view.store_ok:
        return "storage_failed"
    if view.undelivered > 0:
        return "notify_failing"
    live = [o for o in view.occurrences if o.key.profile_id == profile_id and not o.resolved]
    if any(not o.health.fresh or o.health.consecutive_failures >= FAILED_POLLS for o in live):
        return "cannot_see"
    return "ok" if live else "idle"


def sync_entities(
    coordinator: TccCoordinator,
    platform: str,
    async_add_entities: AddEntitiesCallback,
    wanted: Callable[[RuntimeView], dict[str, Callable[[], TccEntity]]],
) -> Callable[[], None]:
    """Adds entities whose unique ID is new and removes this entry's no longer wanted ones."""
    entry = coordinator.config_entry
    assert entry is not None
    added: set[str] = set()

    @callback
    def update() -> None:
        view = coordinator.data
        want = wanted(view)
        if new := [make for uid, make in want.items() if uid not in added]:
            added.update(uid for uid in want if uid not in added)
            async_add_entities([make() for make in new])
        # a view read before the runtime has a config would otherwise delete every entity (HA-17)
        if not view.started or not view.directions:
            return
        # not from the registry: a removed device already took its entries (5.4)
        added.intersection_update(want)
        registry = er.async_get(coordinator.hass)
        for e in er.async_entries_for_config_entry(registry, entry.entry_id):
            if e.domain == platform and e.platform == DOMAIN and e.unique_id not in want:
                registry.async_remove(e.entity_id)

    update()
    return coordinator.async_add_listener(update)

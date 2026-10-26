"""Buttons for the user actions; each calls the same function as its service."""

from collections.abc import Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from train_commute_companion.calendar import TZ
from train_commute_companion.lifecycle import DirectionState
from train_commute_companion.model import Direction
from train_commute_companion.view import RuntimeView

from .coordinator import TccCoordinator
from .data import TccConfigEntry, TccData
from .entity import TccEntity, pick, sync_entities, unique_id
from .services import do_choose, do_leaving, do_not_travelling

PER_DIRECTION = ("leaving", "not_travelling", "choose_next")


class TccButton(TccEntity, ButtonEntity):
    def __init__(
        self,
        coordinator: TccCoordinator,
        data: TccData,
        profile_id: str,
        profile_name: str,
        key: str,
        direction: Direction | None = None,
    ) -> None:
        super().__init__(coordinator, profile_id, profile_name, key, direction)
        self.data = data
        self.key = key
        self.profile_name = profile_name

    async def async_press(self) -> None:
        pid, name, d = self.profile_id, self.profile_name, self.direction
        if self.key == "leaving":
            await do_leaving(self.data, pid, name, None, d)
        elif self.key == "not_travelling":
            await do_not_travelling(self.data, pid, name, None, d, False)
        elif self.key == "not_travelling_today":
            await do_not_travelling(self.data, pid, name, None, None, True)
        else:
            await self._choose_next()

    async def _choose_next(self) -> None:
        assert self.direction is not None
        shown, others = pick(self.coordinator.data, self.profile_id, self.direction)
        occ = next(
            (
                o
                for o in (shown, *others)
                if o is not None
                and o.next_good is not None
                and (o is shown or o.direction_state is not DirectionState.RIDING)
            ),
            None,
        )
        if occ is None or occ.next_good is None:
            raise HomeAssistantError("no next connection to choose now")
        train = occ.next_good.legs[0].train
        await do_choose(
            self.data,
            self.profile_id,
            train.planned_departure.astimezone(TZ).time(),
            train.train_number,
            occ.key.service_date,
            self.direction,
        )


def wanted(
    coordinator: TccCoordinator, data: TccData, view: RuntimeView
) -> dict[str, Callable[[], TccEntity]]:
    out: dict[str, Callable[[], TccEntity]] = {}

    def add(pid: str, name: str, key: str, d: Direction | None = None) -> None:
        out[unique_id(pid, key, d)] = lambda: TccButton(coordinator, data, pid, name, key, d)

    for i in view.directions:
        for key in PER_DIRECTION:
            add(i.profile_id, i.profile_name, key, i.direction)
        add(i.profile_id, i.profile_name, "not_travelling_today")  # same ID from both directions
    return out


async def async_setup_entry(
    hass: HomeAssistant, entry: TccConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = entry.runtime_data
    coordinator = data.coordinator
    entry.async_on_unload(
        sync_entities(
            coordinator, "button", async_add_entities, lambda v: wanted(coordinator, data, v)
        )
    )

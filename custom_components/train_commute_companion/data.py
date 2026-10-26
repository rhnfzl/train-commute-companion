"""What a loaded entry carries. Every shell module imports these from here, so no import cycle."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .coordinator import TccCoordinator
from .host import RuntimeHost
from .location import LocationTracker


@dataclass
class TccData:
    host: RuntimeHost
    coordinator: TccCoordinator
    # retires the runtime, cancels its pending pushes, then stops its thread
    stop: Callable[[], Awaitable[None]]
    tracker: LocationTracker | None = None


type TccConfigEntry = ConfigEntry[TccData]

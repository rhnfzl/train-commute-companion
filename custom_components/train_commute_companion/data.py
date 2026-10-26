"""What a loaded entry carries. Every shell module imports these from here, so no import cycle."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .coordinator import TccCoordinator
from .host import RuntimeHost


@dataclass
class TccData:
    host: RuntimeHost
    coordinator: TccCoordinator
    # retires the runtime, cancels its pending pushes, then stops its thread
    stop: Callable[[], Awaitable[None]]


type TccConfigEntry = ConfigEntry[TccData]

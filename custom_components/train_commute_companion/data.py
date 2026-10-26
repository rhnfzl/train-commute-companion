"""What a loaded entry carries. Every shell module imports these from here, so no import cycle."""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .coordinator import TccCoordinator
from .host import RuntimeHost


@dataclass
class TccData:
    host: RuntimeHost
    coordinator: TccCoordinator


type TccConfigEntry = ConfigEntry[TccData]

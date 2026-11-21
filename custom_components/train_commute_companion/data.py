"""What a loaded entry carries. Every shell module imports these from here, so no import cycle."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .coordinator import TccCoordinator
from .host import RuntimeHost
from .location import LocationTracker
from .notifier import HaNotifier
from .remote import RemoteRuntime


@dataclass(slots=True)
class TccData:
    host: RuntimeHost | RemoteRuntime
    coordinator: TccCoordinator
    # retires the runtime, cancels its pending pushes, then stops its thread
    stop: Callable[[], Awaitable[None]]
    tracker: LocationTracker
    notifier: HaNotifier


type TccConfigEntry = ConfigEntry[TccData]

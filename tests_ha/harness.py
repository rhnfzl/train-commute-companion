"""Drives a real config entry with a scripted NS world and a fake clock that HA time follows."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from builders import AMS, ONE_TRANSFER, only_occurrence  # noqa: F401  (re-exported for tests)
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service
from test_runtime import FakeClock, Provider, at

from custom_components.train_commute_companion import host as host_mod
from custom_components.train_commute_companion import notifier as notifier_mod
from custom_components.train_commute_companion.const import DOMAIN
from custom_components.train_commute_companion.host import RuntimeHost
from train_commute_companion.model import Connection
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.replay import ReplayClock, ReplayProvider

PROFILES = Path(__file__).resolve().parents[1] / "tests" / "data" / "profiles"
PROFILE = (PROFILES / "office_a.yaml").read_text()
STATIONS = frozenset({"AAA", "BBB", "UT", "RTD"})
NOTIFY = "mobile_app_phone"

# set by the tcc_freezer fixture (conftest.py) for the modules built on this harness
FREEZER: FrozenDateTimeFactory | None = None
PATCH: pytest.MonkeyPatch | None = None


class HaProvider(Provider):
    def __init__(self, clock: FakeClock, world: Sequence[Connection]) -> None:
        super().__init__(clock, list(world))
        self.positions: dict[str, tuple[float, float]] = {}

    async def stations(self) -> frozenset[str]:
        return STATIONS


class ReplayStations:
    """A replay's provider with the station list a real one has: `require_stations` would refuse
    the first configuration against the replay provider's empty list."""

    positions: dict[str, tuple[float, float]]

    def __init__(self, inner: ReplayProvider, codes: frozenset[str]) -> None:
        self.inner, self.codes, self.positions = inner, codes, {}

    async def trips(self, q: Query) -> ProviderAnswer:
        return await self.inner.trips(q)

    async def planned_works(self) -> PlannedWorks:
        return await self.inner.planned_works()

    async def stations(self) -> frozenset[str]:
        return self.codes


def fake_phone(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    name: str = NOTIFY,
    handler: Callable[[ServiceCall], Awaitable[None]] | None = None,
    **mock_kwargs: Any,
) -> list[ServiceCall]:
    """A notify service the notifier treats as a mobile_app phone: the real phone path's
    contract (raise on failure, a timeout re-raised as HomeAssistantError from the TimeoutError,
    as mobile_app's relay helper does) without a mobile_app entry."""
    if handler is None:
        calls = async_mock_service(hass, "notify", name, **mock_kwargs)
    else:
        hass.services.async_register("notify", name, handler)
        calls = []
    real_target = notifier_mod.mobile_target

    def target(h: HomeAssistant, service: str) -> str | None:
        return f"hook-{name}" if service == name else real_target(h, service)

    async def push(
        h: HomeAssistant, webhook_id: str, payload: dict[str, Any], *_a: Any, **_k: Any
    ) -> bool:
        try:
            async with asyncio.timeout(notifier_mod.NOTIFY_TIMEOUT_S):
                await h.services.async_call(
                    "notify", webhook_id.removeprefix("hook-"), payload, blocking=True
                )
        except TimeoutError as e:
            raise HomeAssistantError("timeout sending notification") from e
        return True

    monkeypatch.setattr(notifier_mod, "mobile_target", target)
    monkeypatch.setattr(notifier_mod, "_push_mobile", push)
    return calls


@dataclass
class Harness:
    entry: MockConfigEntry
    host: RuntimeHost
    clock: FakeClock
    provider: HaProvider
    pushes: list[ServiceCall]

    @property
    def hass(self) -> HomeAssistant:
        return self.entry.runtime_data.coordinator.hass


def profiles_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(DOMAIN, "profiles.yaml"))


def state_path(hass: HomeAssistant, entry: MockConfigEntry) -> Path:
    return Path(hass.config.path(DOMAIN, f"state-{entry.entry_id}.json"))


def write_profiles(hass: HomeAssistant, text: str) -> None:
    path = profiles_path(hass)
    os.makedirs(path.parent, exist_ok=True)
    path.write_text(text)


def prepare(
    hass: HomeAssistant,
    profile_text: str,
    now: str,
    world: Sequence[Connection],
    *,
    replay: tuple[ReplayClock, ReplayStations] | None = None,
) -> tuple[MockConfigEntry, FakeClock, HaProvider, list[ServiceCall]]:
    """Everything `setup_tcc` does before setting the entry up. `replay` swaps in a replay's
    clock and provider for the scripted ones."""
    assert FREEZER is not None and PATCH is not None, "use pytest.mark.usefixtures('tcc_freezer')"
    clock = FakeClock(at(now))
    provider = HaProvider(clock, world)
    if replay is not None:
        clock, provider = replay  # type: ignore[assignment]
    FREEZER.move_to(clock.now())
    write_profiles(hass, profile_text)
    pushes = fake_phone(hass, PATCH)

    async def close() -> None:
        pass

    PATCH.setattr(host_mod, "AUTORUN", False)
    PATCH.setattr(host_mod, "make_clock", lambda: clock)
    PATCH.setattr(host_mod, "make_provider", lambda api_key, c: (provider, close))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "k"},
        options={
            "profiles_path": str(profiles_path(hass)),
            "notify_service": NOTIFY,
            "loud_style": "time_sensitive",
        },
    )
    entry.add_to_hass(hass)
    return entry, clock, provider, pushes


async def _start(h: Harness) -> None:
    # the first refresh ran before the runtime had a config
    await h.host.run(lambda rt: rt.start())
    await h.entry.runtime_data.coordinator.async_refresh()
    await h.hass.async_block_till_done()


async def setup_tcc(
    hass: HomeAssistant,
    profile_text: str,
    *,
    now: str = "07:50",
    world: Sequence[Connection] = ONE_TRANSFER,
    start: bool = True,
) -> Harness:
    entry, clock, provider, pushes = prepare(hass, profile_text, now, world)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    h = Harness(entry, entry.runtime_data.host, clock, provider, pushes)
    if start:
        await _start(h)
    return h


async def drive(h: Harness, until: str) -> None:
    """Steps both clocks by 30 s up to `until` on the current day: a poll on even minutes, the
    watchdog every step, then a coordinator refresh."""
    assert FREEZER is not None
    end = at(until, h.clock.now().date())
    coordinator = h.entry.runtime_data.coordinator
    while h.clock.now() < end:
        h.clock.jump(0.5)
        FREEZER.tick(30)
        t = h.clock.now()
        if t.minute % 2 == 0 and t.second == 0:
            await h.host.run(lambda rt: rt.poll_once())
        await h.host.run(lambda rt: rt.watchdog_once())
        await coordinator.async_refresh()
    await h.hass.async_block_till_done()


async def jump(h: Harness, when: str) -> None:
    """Sets both clocks to "YYYY-MM-DD HH:MM", with no poll."""
    assert FREEZER is not None
    h.clock.t = datetime.fromisoformat(when).replace(tzinfo=AMS)
    FREEZER.move_to(h.clock.t)


async def rebound(h: Harness) -> Harness:
    """The same harness on the reloaded entry's runtime, started and refreshed."""
    assert h.entry.state is ConfigEntryState.LOADED
    n = replace(h, host=h.entry.runtime_data.host)
    await _start(n)
    return n

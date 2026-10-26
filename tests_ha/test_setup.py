import asyncio
import threading
import time as wall
from dataclasses import replace
from datetime import date, time
from unittest.mock import AsyncMock, Mock

import harness
import pytest
from harness import (
    NOTIFY,
    PROFILE,
    PROFILES,
    drive,
    only_occurrence,
    prepare,
    rebound,
    setup_tcc,
    state_path,
    write_profiles,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.train_commute_companion import async_unload_entry
from custom_components.train_commute_companion import coordinator as coordinator_mod
from custom_components.train_commute_companion import host as host_mod
from custom_components.train_commute_companion.const import CONF_STARTED, DOMAIN
from custom_components.train_commute_companion.coordinator import TccCoordinator
from train_commute_companion.lookahead import (
    BaselineHistory,
    BaselineVersion,
    Signature,
    SignatureLeg,
)
from train_commute_companion.store import JsonFileStore, StorageError

pytestmark = pytest.mark.usefixtures("tcc_freezer")

OVERLAP = (PROFILES / "both_on_wed.yaml").read_text()


def no_runtime_thread() -> bool:
    return not any(t.name == "tcc-runtime" for t in threading.enumerate())


def texts(h) -> list[str]:
    return [c.data["message"] for c in h.pushes]


def loud(call) -> bool:
    return call.data["data"]["push"]["interruption-level"] == "time-sensitive"


async def test_setup_and_unload_leave_no_runtime_thread(hass):
    h = await setup_tcc(hass, PROFILE)
    assert h.entry.state is ConfigEntryState.LOADED and h.host.alive
    assert not no_runtime_thread()
    assert await hass.config_entries.async_unload(h.entry.entry_id)
    assert h.entry.state is ConfigEntryState.NOT_LOADED
    assert no_runtime_thread()


async def test_failed_setup_leaves_no_runtime_thread(hass):
    entry, *_ = prepare(hass, PROFILE, "07:50", ())

    def broken(api_key, clock):
        raise ValueError("no provider")

    harness.PATCH.setattr(host_mod, "make_provider", broken)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert no_runtime_thread()
    await hass.config_entries.async_remove(entry.entry_id)

    entry2, *_ = prepare(hass, PROFILE, "07:50", ())
    harness.PATCH.setattr(
        TccCoordinator, "_async_update_data", AsyncMock(side_effect=RuntimeError("boom"))
    )
    assert not await hass.config_entries.async_setup(entry2.entry_id)
    assert entry2.state is ConfigEntryState.SETUP_RETRY
    assert no_runtime_thread()


async def test_entry_reload_keeps_lock_and_sends_no_repeat(hass):
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "08:00")
    before = len(h.pushes)

    def trains(rt):
        lock = only_occurrence(rt).lock
        return lock and [x.train for x in lock.legs]

    lock = await h.host.run(trains)
    assert lock
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    h = await rebound(h)
    await drive(h, "08:02")
    assert await h.host.run(trains) == lock
    assert len(h.pushes) == before, [c.data["message"] for c in h.pushes]


async def test_first_install_is_silent(hass):
    h = await setup_tcc(hass, PROFILE)
    assert not any("saved state" in t or "could not save" in t for t in texts(h))
    assert h.entry.data[CONF_STARTED] is True


async def test_missing_store_after_a_start_is_storage_failed(hass):
    h = await setup_tcc(hass, PROFILE)
    assert h.entry.data[CONF_STARTED] is True
    assert await hass.config_entries.async_unload(h.entry.entry_id)
    path = state_path(hass, h.entry)
    # the store's own marker goes too: only the entry still knows this integration ran before
    path.unlink()
    path.with_name(path.name + ".started").unlink()
    assert await hass.config_entries.async_setup(h.entry.entry_id)
    h = await rebound(h)
    lost = [c for c in h.pushes if "saved state was lost" in c.data["message"]]
    assert len(lost) == 1 and loud(lost[0])  # default loud style
    assert "this integration ran before" in lost[0].data["message"]


async def test_corrupt_store_is_storage_failed_and_kept_aside(hass):
    h = await setup_tcc(hass, PROFILE)
    assert await hass.config_entries.async_unload(h.entry.entry_id)
    path = state_path(hass, h.entry)
    path.write_bytes(b"not json {")
    assert await hass.config_entries.async_setup(h.entry.entry_id)
    h = await rebound(h)
    lost = [c for c in h.pushes if "saved state was lost" in c.data["message"]]
    assert len(lost) == 1 and loud(lost[0])
    broken = list(path.parent.glob(path.name + ".broken-*"))
    assert len(broken) == 1 and broken[0].read_bytes() == b"not json {"


async def test_failed_store_write_is_storage_failed(hass):
    h = await setup_tcc(hass, PROFILE)

    def fail(self, s):
        raise StorageError("disk full")

    harness.PATCH.setattr(JsonFileStore, "save", fail)
    await drive(h, "08:06")
    failed = [c for c in h.pushes if "could not save state" in c.data["message"]]
    assert len(failed) == 1 and loud(failed[0])
    assert h.entry.runtime_data.coordinator.data.store_ok is False


async def test_refused_edit_raises_repair_and_loud_push_then_clears(hass):
    h = await setup_tcc(hass, PROFILE)
    coordinator = h.entry.runtime_data.coordinator
    write_profiles(hass, OVERLAP)
    await h.host.run(lambda rt: rt.check_file())
    await coordinator.async_refresh()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "profile_refused")
    assert issue is not None and issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders["reason"]
    assert "profile change refused" in h.pushes[-1].data["message"] and loud(h.pushes[-1])
    write_profiles(hass, PROFILE)
    await h.host.run(lambda rt: rt.check_file())
    await coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "profile_refused") is None


async def test_moved_prompt_repair(hass):
    h = await setup_tcc(hass, PROFILE)
    sig = Signature(
        (
            SignatureLeg("AAA", "UT", time(8, 15), 0, 28),
            SignatureLeg("UT", "RTD", time(8, 50), 0, 34),
        )
    )
    hist = BaselineHistory((BaselineVersion(1, sig, date(2026, 11, 3), True),))

    def seed(baselines):
        def go(rt):
            rt._st = replace(rt.state, baselines=baselines)

        return go

    await h.host.run(seed({"p1|outbound": hist}))
    await h.entry.runtime_data.coordinator.async_refresh()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "moved_p1_outbound")
    assert issue is not None and issue.translation_key == "moved"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert "NS moved your 08:12 to 08:15" in issue.translation_placeholders["message"]
    await h.host.run(seed({}))
    await h.entry.runtime_data.coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "moved_p1_outbound") is None


async def test_undelivered_alert_raises_notify_failing_repair(hass):
    h = await setup_tcc(hass, PROFILE)
    coordinator = h.entry.runtime_data.coordinator
    hass.services.async_remove("notify", NOTIFY)
    write_profiles(hass, OVERLAP)
    await h.host.run(lambda rt: rt.check_file())
    await coordinator.async_refresh()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing")
    assert issue is not None and issue.translation_placeholders == {"service": NOTIFY}
    back = async_mock_service(hass, "notify", NOTIFY)
    await h.host.run(lambda rt: rt.watchdog_once())
    await coordinator.async_refresh()
    assert any("profile change refused" in c.data["message"] for c in back)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing") is None


async def test_dead_runtime_schedules_reload(hass):
    h = await setup_tcc(hass, PROFILE)
    coordinator = h.entry.runtime_data.coordinator
    reload = Mock()
    harness.PATCH.setattr(hass.config_entries, "async_schedule_reload", reload)
    harness.PATCH.setattr(host_mod.RuntimeHost, "alive", property(lambda self: False))
    await coordinator.async_refresh()
    reload.assert_called_once_with(h.entry.entry_id)
    assert coordinator.last_update_success is False


async def test_runtime_not_answering_fails_refresh_then_reloads(hass):
    h = await setup_tcc(hass, PROFILE)
    coordinator = h.entry.runtime_data.coordinator
    reload = Mock()
    harness.PATCH.setattr(hass.config_entries, "async_schedule_reload", reload)

    async def never(fn):
        await asyncio.Event().wait()

    harness.PATCH.setattr(h.host, "run", never)
    for n in range(1, 5):
        refresh = hass.async_create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        # hass.loop reads the frozen clock, so its timeout fires only when the freezer moves
        harness.FREEZER.tick(coordinator_mod.RUN_TIMEOUT_S + 1)
        await refresh
        assert coordinator.last_update_success is False
        assert reload.call_count == (1 if n == 4 else 0)


async def test_ha_stop_stops_the_runtime_without_unload(hass):
    h = await setup_tcc(hass, PROFILE)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert no_runtime_thread()
    assert h.entry.state is ConfigEntryState.LOADED


async def test_failed_platform_unload_keeps_host_and_tracker(hass):
    h = await setup_tcc(hass, PROFILE)
    unload = AsyncMock(return_value=False)
    harness.PATCH.setattr(hass.config_entries, "async_unload_platforms", unload)
    assert await async_unload_entry(hass, h.entry) is False
    assert h.host.alive
    unload.side_effect = RuntimeError("platform refused")
    with pytest.raises(RuntimeError):
        await async_unload_entry(hass, h.entry)
    assert h.host.alive
    harness.PATCH.undo()  # the fixture's unload then works for real


class YieldingClock:
    """A fixed instant whose sleep yields for real, so run_forever lets its loop run."""

    def __init__(self, t) -> None:
        self.t = t

    def now(self):
        return self.t

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0.01)


async def test_autorun_setup_starts_and_unloads(hass):
    entry, clock, *_ = prepare(hass, PROFILE, "07:50", ())
    harness.PATCH.setattr(host_mod, "AUTORUN", True)
    harness.PATCH.setattr(host_mod, "make_clock", lambda: YieldingClock(clock.now()))
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    for _ in range(300):
        await coordinator.async_refresh()
        if coordinator.data.started:
            break
        await hass.async_add_executor_job(wall.sleep, 0.01)  # hass.loop timers are frozen
    assert coordinator.data.started
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert no_runtime_thread()


async def test_runtime_loop_timers_run_under_freezer(hass, tcc_freezer):
    h = await setup_tcc(hass, PROFILE)
    await h.host.run(lambda rt: asyncio.sleep(0.01))

    async def times_out() -> bool:
        try:
            async with asyncio.timeout(0.05):
                await asyncio.Event().wait()
        except TimeoutError:
            return True
        return False

    assert await h.host.run(lambda rt: times_out())

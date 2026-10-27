import asyncio
import logging
import os
import threading
import time as wall
from dataclasses import replace
from datetime import date, time
from unittest.mock import AsyncMock, Mock

import harness
import httpx
import pytest
from builders import make_leg
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
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)
from test_config_flow import HOOKS, SECRET, URL, open_webhooks, stored_copies, submit
from webhook_receiver import Receiver

from custom_components.train_commute_companion import async_setup_entry, async_unload_entry
from custom_components.train_commute_companion import coordinator as coordinator_mod
from custom_components.train_commute_companion import host as host_mod
from custom_components.train_commute_companion import notifier as notifier_mod
from custom_components.train_commute_companion.const import CONF_STARTED, DOMAIN, REFRESH_S
from custom_components.train_commute_companion.coordinator import TccCoordinator
from train_commute_companion import store as core_store
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


async def test_view_before_start_keeps_repairs(hass):
    # AUTORUN: the first refresh can read the view before start; it must not delete issues
    reg = ir.async_get(hass)
    for issue_id in ("moved_p1_outbound", "notify_failing", "profile_refused"):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="moved",
            translation_placeholders={"message": "m"},
        )
    reg.async_ignore(DOMAIN, "moved_p1_outbound", True)
    h = await setup_tcc(hass, PROFILE, start=False)
    assert not h.entry.runtime_data.coordinator.data.started
    for issue_id in ("moved_p1_outbound", "notify_failing", "profile_refused"):
        assert reg.async_get_issue(DOMAIN, issue_id) is not None
    assert reg.async_get_issue(DOMAIN, "moved_p1_outbound").dismissed_version is not None


async def test_undelivered_alert_raises_notify_failing_repair(hass):
    h = await setup_tcc(hass, PROFILE)
    coordinator = h.entry.runtime_data.coordinator
    hass.services.async_remove("notify", NOTIFY)
    write_profiles(hass, OVERLAP)
    await h.host.run(lambda rt: rt.check_file())
    await coordinator.async_refresh()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing")
    assert issue is not None and issue.translation_placeholders == {"service": NOTIFY}
    back = harness.fake_phone(hass, harness.PATCH)
    await h.host.run(lambda rt: rt.watchdog_once())
    await coordinator.async_refresh()
    assert any("profile change refused" in c.data["message"] for c in back)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing") is None


@pytest.mark.parametrize("upgrade", [False, True])
async def test_vanished_phone_target_raises_notify_failing(hass, upgrade):
    # upgrade: the stored target is a user-made mobile_app service, never a phone
    stray = async_mock_service(hass, "notify", "mobile_app_fake")
    if upgrade:
        harness.PATCH.setattr(harness, "NOTIFY", "mobile_app_fake")
    h = await setup_tcc(hass, PROFILE)
    before = len(h.pushes)
    if not upgrade:  # the phone is gone: HA no longer maps the service to a mobile_app entry
        harness.PATCH.setattr(notifier_mod, "mobile_target", lambda hass, service: None)
    write_profiles(hass, OVERLAP)
    await h.host.run(lambda rt: rt.check_file())
    await tick(hass, REFRESH_S + 1)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing") is not None
    assert not stray and len(h.pushes) == before


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
    for n in range(1, 6):
        refresh = hass.async_create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        # hass.loop reads the frozen clock, so its timeout fires only when the freezer moves
        harness.FREEZER.tick(coordinator_mod.RUN_TIMEOUT_S + 1)
        await refresh
        assert coordinator.last_update_success is False
        # keeps asking after the fourth, in case the first reload did not replace it
        assert reload.call_count == max(0, n - 3)


async def test_ha_stop_stops_the_runtime_without_unload(hass):
    h = await setup_tcc(hass, PROFILE)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert no_runtime_thread()
    assert h.entry.state is ConfigEntryState.LOADED


async def test_failed_platform_unload_keeps_host_and_tracker(hass):
    h = await setup_tcc(hass, PROFILE)
    tracker = h.entry.runtime_data.tracker
    restart = Mock(wraps=tracker.start)
    harness.PATCH.setattr(tracker, "start", restart)
    unload = AsyncMock(return_value=False)
    harness.PATCH.setattr(hass.config_entries, "async_unload_platforms", unload)
    assert await async_unload_entry(hass, h.entry) is False
    assert h.host.alive and restart.call_count == 1 and tracker._unsub_data is not None
    unload.side_effect = RuntimeError("platform refused")
    with pytest.raises(RuntimeError):
        await async_unload_entry(hass, h.entry)
    assert h.host.alive and restart.call_count == 2 and tracker._unsub_data is not None
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


async def tick(hass, seconds: float) -> None:
    """Moves HA time on so its own scheduled refresh runs, with no manual refresh."""
    harness.FREEZER.tick(seconds)
    async_fire_time_changed(hass)
    # the scheduled refresh is a background task waiting on the runtime thread
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_monitoring_runs_without_manual_refresh(hass):
    h = await setup_tcc(hass, PROFILE)
    hass.services.async_remove("notify", NOTIFY)
    write_profiles(hass, OVERLAP)
    await h.host.run(lambda rt: rt.check_file())
    await tick(hass, REFRESH_S + 1)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing") is not None

    reload = Mock()
    harness.PATCH.setattr(hass.config_entries, "async_schedule_reload", reload)
    harness.PATCH.setattr(host_mod.RuntimeHost, "alive", property(lambda self: False))
    await tick(hass, REFRESH_S + 1)
    reload.assert_called_with(h.entry.entry_id)


async def test_blocked_runtime_reloads_and_is_retired(hass):
    h = await setup_tcc(hass, PROFILE)
    harness.PATCH.setattr(host_mod, "JOIN_TIMEOUT_S", 0.2)
    old = h.host
    gate, entered = threading.Event(), threading.Event()

    def stuck_then_try(rt):
        entered.set()
        gate.wait(30)  # a synchronous call that never yields: drain cannot run either
        try:
            rt._store.save(rt.state)
        except StorageError:
            return False
        return True

    stuck = hass.async_create_task(old.run(stuck_then_try))
    assert await hass.async_add_executor_job(entered.wait, 5)  # hass.loop timers are frozen
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    assert h.entry.state is ConfigEntryState.LOADED
    new = h.entry.runtime_data.host
    assert new is not old and new.alive and not old.alive
    with pytest.raises(RuntimeError):
        await old.run(lambda rt: None)

    # it wakes after its successor took over: its store refuses the write
    gate.set()
    assert await stuck is False
    await hass.async_add_executor_job(old._thread.join, 5)
    assert sum(t.name == "tcc-runtime" for t in threading.enumerate()) == 1
    await old.stop()  # the later stop finishes the cleanup it kept
    assert old._thread is None


def pause_old_thread(old_thread, gate, entered, fn, then=None):
    """`fn`, but on the old runtime thread it waits for `gate` first, then calls `then` if given."""

    def wrapped(*args):
        if threading.current_thread() is old_thread:
            entered.set()
            gate.wait(30)
            if then is not None:
                return then(*args)
        return fn(*args)

    return wrapped


async def start_old_storage_call(hass, h, call):
    """Runs `call` on the old runtime, to be paused inside storage. Returns its task and an event
    set once any other thread reads the state file."""
    harness.PATCH.setattr(host_mod, "JOIN_TIMEOUT_S", 0.2)
    old_thread = h.host._thread
    early = threading.Event()
    real_load = JsonFileStore.load

    def watched_load(self):
        if threading.current_thread() is not old_thread:
            early.set()
        return real_load(self)

    harness.PATCH.setattr(JsonFileStore, "load", watched_load)
    stuck = hass.async_create_task(h.host.run(call))
    return stuck, early


async def test_reload_while_the_old_runtime_is_inside_a_save(hass):
    h = await setup_tcc(hass, PROFILE)
    old = h.host
    gate, entered = threading.Event(), threading.Event()
    # paused after the counter check and before the commit, the window a successor raced into
    harness.PATCH.setattr(os, "fsync", pause_old_thread(old._thread, gate, entered, os.fsync))

    def save(rt):
        rt._store.save(rt.state)
        return True

    stuck, early = await start_old_storage_call(hass, h, save)
    assert await hass.async_add_executor_job(entered.wait, 5)
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    successor = hass.async_create_task(rebound(h))
    # the successor waits for the old write to finish instead of reading under it
    assert not await hass.async_add_executor_job(early.wait, 0.5)
    gate.set()
    assert await stuck is True  # it passed the retirement check before the stop, so it completes
    h = await successor
    assert early.is_set()
    await drive(h, "08:02")
    assert not any("could not save" in t for t in texts(h))
    assert h.entry.runtime_data.coordinator.data.store_ok is True
    await hass.async_add_executor_job(old._thread.join, 5)
    await old.stop()


async def test_reload_while_the_old_runtime_is_inside_a_load(hass):
    h = await setup_tcc(hass, PROFILE)
    old = h.host
    gate, entered = threading.Event(), threading.Event()
    path = state_path(hass, h.entry)

    def unreadable(*_):
        raise ValueError("unreadable")

    # the old reader finds the file corrupt, then sleeps before moving it aside
    harness.PATCH.setattr(
        core_store,
        "decode",
        pause_old_thread(old._thread, gate, entered, core_store.decode, unreadable),
    )
    stuck, early = await start_old_storage_call(hass, h, lambda rt: rt._store.load())
    assert await hass.async_add_executor_job(entered.wait, 5)
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    successor = hass.async_create_task(rebound(h))
    assert not await hass.async_add_executor_job(early.wait, 0.5)
    gate.set()
    with pytest.raises(StorageError, match="corrupt"):
        await stuck
    h = await successor
    # the successor saw the old reader's verdict, and the file it then wrote stays in place
    assert any("saved state was lost" in t for t in texts(h))
    await drive(h, "08:02")
    assert not any("could not save" in t for t in texts(h))
    assert h.entry.runtime_data.coordinator.data.store_ok is True
    assert len(list(path.parent.glob(path.name + ".broken-*"))) == 1 and path.exists()
    await hass.async_add_executor_job(old._thread.join, 5)
    await old.stop()


async def test_cancelled_setup_leaves_no_runtime_thread(hass):
    entry, *_ = prepare(hass, PROFILE, "07:50", ())
    harness.PATCH.setattr(host_mod, "JOIN_TIMEOUT_S", 5)
    gate, entered = threading.Event(), threading.Event()
    real = host_mod.make_provider

    def slow(api_key, clock):
        entered.set()
        gate.wait(10)
        return real(api_key, clock)

    harness.PATCH.setattr(host_mod, "make_provider", slow)
    task = hass.async_create_task(async_setup_entry(hass, entry))
    assert await hass.async_add_executor_job(entered.wait, 5)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert no_runtime_thread()


# --- webhooks ---


def cancel_first_train(h) -> None:
    first = h.provider.world[0]
    gone = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True)
    h.provider.world[0] = replace(first, legs=(gone, *first.legs[1:]))


async def posted(h) -> None:
    """Waits for the runtime's webhook pass, then refreshes HA's view."""
    await h.host.run(lambda rt: rt.webhooks_idle())
    await h.entry.runtime_data.coordinator.async_refresh()


async def with_hooks(hass, status=200, **options):
    rx = Receiver(SECRET, None, status)
    h = await setup_tcc(hass, PROFILE, options={**HOOKS, **options}, receiver=rx)
    await posted(h)
    return h, rx


async def test_alert_reaches_phone_and_signed_webhook(hass):
    h, rx = await with_hooks(hass)
    pushes, posts = len(h.pushes), len(rx.posts)
    cancel_first_train(h)
    await drive(h, "08:06")
    await posted(h)
    new, hooks = h.pushes[pushes:], rx.posts[posts:]
    assert rx.bad == 0 and len(new) == 1 and len(hooks) == 1
    assert hooks[0]["text"] == new[0].data["message"] and "cancelled" in hooks[0]["text"]
    assert hooks[0]["severity"] == "loud" and loud(new[0]) and hooks[0]["test"] is False


async def test_unload_closes_the_webhook_client(hass):
    h, rx = await with_hooks(hass)
    assert len(h.clients) == 1 and not h.clients[0].is_closed
    before = len(rx.requests)
    assert await hass.config_entries.async_unload(h.entry.entry_id)
    assert h.clients[0].is_closed and len(rx.requests) == before


async def test_without_webhook_options_no_client_is_made(hass):
    def refuse() -> httpx.AsyncClient:
        raise AssertionError("no webhook client without webhook options")

    harness.PATCH.setattr(host_mod, "make_webhook_client", refuse)
    h = await setup_tcc(hass, PROFILE)
    assert h.entry.state is ConfigEntryState.LOADED and h.host.alive
    assert await h.host.run(lambda rt: rt._webhooks) is None


async def test_options_turning_webhooks_off_drops_saved_copies(hass):  # Review Focus 13
    h, rx = await with_hooks(hass, status=503)
    cancel_first_train(h)
    await drive(h, "08:06")
    await posted(h)
    assert stored_copies(hass, h.entry)  # a copy is owed
    tried = len(rx.requests)
    r = await submit(hass, await open_webhooks(hass, h.entry))  # no URLs: no test send
    assert r["type"] is FlowResultType.CREATE_ENTRY and len(rx.requests) == tried
    h = await rebound(h)
    assert await h.host.run(lambda rt: rt._webhooks) is None
    await drive(h, "08:08")
    assert stored_copies(hass, h.entry) == ()
    rx.status = 200
    r = await submit(hass, await open_webhooks(hass, h.entry), URL, SECRET)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert [p["test"] for p in rx.posts[tried:]] == [True, True]
    h = await rebound(h)
    await drive(h, "08:12")
    await posted(h)
    assert len(rx.requests) == tried + 2  # the old alert is never posted
    assert stored_copies(hass, h.entry) == ()


async def test_dead_webhook_raises_no_notify_failing(hass):  # Review Focus 1
    h, rx = await with_hooks(hass, status=503)
    pushes = len(h.pushes)
    cancel_first_train(h)
    await drive(h, "08:06")
    await posted(h)
    assert len(h.pushes) == pushes + 1 and rx.requests
    assert h.entry.runtime_data.coordinator.data.undelivered == 0
    assert ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing") is None


async def test_ha_logs_never_show_the_webhook_path(hass, caplog):  # Review Focus 5
    caplog.set_level(logging.DEBUG, "httpx")
    caplog.set_level(logging.DEBUG, "httpcore")
    route = "https://hooks.example.invalid/tcc/route-token-123"
    h, rx = await with_hooks(hass, status=503, webhook_urls=[route])
    cancel_first_train(h)
    await drive(h, "08:06")
    await posted(h)
    failed = len(rx.requests)
    rx.status = 200
    await drive(h, "08:10")  # after the 60 s rest
    await posted(h)
    assert failed and len(rx.requests) > failed
    assert all(q.url.path == "/tcc/route-token-123" for q in rx.requests)
    warned = "webhook to hooks.example.invalid failed (503)"
    assert any(warned in r.getMessage() for r in caplog.records)  # the records were captured
    for record in caplog.records:
        assert "route-token-123" not in record.getMessage() and SECRET not in record.getMessage()

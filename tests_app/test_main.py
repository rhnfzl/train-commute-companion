import asyncio
import dataclasses
import logging
import socket
from datetime import time
from pathlib import Path

import pytest
from builders import ONE_TRANSFER
from conftest import CHAT, TOKEN, FakeTelegram, StationsProvider, TickClock
from tcc_app.main import App, Settings, build, main, serve, setup_logging
from test_runtime import PROFILE, Provider, at
from webhook_receiver import Receiver

from train_commute_companion.lifecycle import Leaving
from train_commute_companion.model import EventKind
from train_commute_companion.runtime import WebhookPoster
from train_commute_companion.store import JsonFileStore

ONE_PROFILE = PROFILE.split('  - id: "p2"')[0].replace("NAME", "office_a")
CODES = frozenset({"AAA", "UT", "RTD"})
ENV = {"NS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": TOKEN}
URL, URL2 = "https://hooks.example.invalid/tcc", "https://hooks2.example.invalid/tcc"
SECRET, WTOKEN = "test-webhook-secret-0001", "test-webhook-token-0001"


def make_app(tmp_path: Path, tg: FakeTelegram, clock: TickClock, provider=None,
             lookahead_at: time = time(6, 0), **settings) -> App:  # fmt: skip
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(ONE_PROFILE)
    s = Settings("k", TOKEN, CHAT, None, profiles, tmp_path, lookahead_at)
    s = dataclasses.replace(s, **settings)
    if provider is None:
        provider = StationsProvider(Provider(clock, list(ONE_TRANSFER)), CODES)
    return build(s, clock=clock, provider=provider, client=tg.client())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def refused(port: int) -> bool:
    try:
        _, w = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    w.close()
    return False


async def until(cond, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not cond():
            await asyncio.sleep(0.005)


def test_settings_from_env_defaults_and_refusals():
    s = Settings.from_env(ENV)
    assert (s.chat_id, s.owntracks_secret, s.profiles, s.state_dir) == (
        None, None, Path("/config/profiles.yaml"), Path("/data")
    )  # fmt: skip
    assert s.lookahead_at == time(6, 0)
    with pytest.raises(ValueError, match="NS_API_KEY is not set"):
        Settings.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN is not set"):
        Settings.from_env({"NS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "  "})
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID") as e:
        Settings.from_env({**ENV, "TELEGRAM_CHAT_ID": "abc"})
    assert "abc" not in str(e.value)
    s = Settings.from_env({**ENV, "TELEGRAM_CHAT_ID": "1000001", "OWNTRACKS_SECRET": "test-secret",
                           "TCC_PROFILES": "/x/p.yaml", "TCC_STATE_DIR": "/y"})  # fmt: skip
    assert (s.chat_id, s.owntracks_secret, s.profiles, s.state_dir) == (
        CHAT, "test-secret", Path("/x/p.yaml"), Path("/y")
    )  # fmt: skip
    s = Settings.from_env({**ENV, "TCC_LOOKAHEAD_AT": "05:30"})
    assert s.lookahead_at == time(5, 30)
    with pytest.raises(ValueError, match="TCC_LOOKAHEAD_AT") as e:
        Settings.from_env({**ENV, "TCC_LOOKAHEAD_AT": "25:00"})
    assert "25:00" not in str(e.value)


def test_settings_health_token_optional_and_checked():
    assert Settings.from_env(ENV).health_token is None
    assert Settings.from_env(
        {**ENV, "TCC_HEALTH_TOKEN": "test-health-token-0001"}
    ).health_token == ("test-health-token-0001")
    for bad in ["short", "tést-health-token-01", "test-health token-01",
                "test-health-token\n01", "test-health-token\x0701"]:  # fmt: skip
        with pytest.raises(ValueError, match="TCC_HEALTH_TOKEN") as e:
            Settings.from_env({**ENV, "TCC_HEALTH_TOKEN": bad})
        assert bad not in str(e.value)


async def test_build_passes_the_lookahead_time(tmp_path, tg):
    clock = TickClock(at("05:20"))
    app = make_app(tmp_path, tg, clock, lookahead_at=time(5, 30))
    calls = []
    real = app.rt.lookahead_once

    async def spy():
        calls.append(clock.now())
        await real()

    app.rt.lookahead_once = spy
    task = asyncio.create_task(app.rt.run_forever())
    try:
        await until(lambda: calls)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert at("05:30") <= calls[0] < at("05:45")  # the default would wait until 06:00


def test_main_refuses_without_key(monkeypatch, capsys):
    monkeypatch.delenv("NS_API_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert main() == 1
    assert "NS_API_KEY is not set; refusing to start" in capsys.readouterr().out


def test_setup_logging_keeps_httpx_quiet():
    setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


async def test_watchdog_runs_while_station_list_hangs(tmp_path, tg):  # plan-1 DEFER 8
    clock = TickClock(at("07:50"))
    asked, never = asyncio.Event(), asyncio.Event()

    class Hanging(StationsProvider):
        async def stations(self) -> frozenset[str]:
            asked.set()
            await never.wait()
            return CODES

    app = make_app(tmp_path, tg, clock, Hanging(Provider(clock, list(ONE_TRANSFER)), CODES))
    ticks = []
    real = app.rt.watchdog_once

    async def spy():
        ticks.append(asked.is_set() and not never.is_set())
        await real()

    app.rt.watchdog_once = spy
    task = asyncio.create_task(serve(app, free_port()))
    await until(lambda: asked.is_set() and any(ticks))
    assert not app.rt.started  # still inside start, waiting for the station list
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_runtime_crash_ends_serve_with_1(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock)
    ticks = []

    async def boom():
        await asyncio.sleep(0.05)  # the watchdog is running by now
        raise RuntimeError("start failed")

    async def tick():
        ticks.append(clock.now())

    app.rt.start = boom
    app.rt.watchdog_once = tick
    before = asyncio.all_tasks()
    port = free_port()
    assert await asyncio.wait_for(serve(app, port), 5) == 1
    assert ticks  # the watchdog every() ran, and is gone now
    assert await refused(port)
    assert asyncio.all_tasks() - before == set()


async def test_cancel_closes_server_and_parts(tmp_path, tg):  # Review Focus 5
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock)
    before = asyncio.all_tasks()
    port = free_port()
    task = asyncio.create_task(serve(app, port))
    await until(lambda: app.rt.started and (tmp_path / "telegram.json").exists())
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n")  # never finished
    await writer.drain()
    await asyncio.sleep(0.05)  # its handler is waiting for the rest
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await refused(port)
    assert await asyncio.wait_for(reader.read(), 1) == b""  # the handler closed it, unanswered
    writer.close()
    assert asyncio.all_tasks() - before == set()


async def test_action_cancelled_mid_delivery_is_already_written(tmp_path, tg):
    """serve's cleanup cancels the Bot's runtime calls: an action is written before its first
    await (rule 22), so a cancel during the delivery pass leaves file and memory equal."""
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock)

    async def refuse(event):
        return False

    app.notifier.send = refuse
    await app.rt.start()  # its message stays pending
    entered = asyncio.Event()

    async def hang(event):
        entered.set()
        await asyncio.Event().wait()

    app.notifier.send = hang
    store = JsonFileStore(tmp_path / "tcc-state.json")
    before = store.load()
    task = asyncio.create_task(app.rt.act("p1", Leaving(None, clock.now())))
    await asyncio.wait_for(entered.wait(), 2)  # inside the delivery pass, after the action
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = store.load()
    assert after != before  # the Leaving tap was written
    assert after == app.rt.state  # nothing changed in memory only
    assert after is not None and after.obligations  # the unsent message is still owed


def test_settings_webhooks_optional_and_checked():
    base = {"NS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": TOKEN}
    assert Settings.from_env(base).webhook_urls == ()
    s = Settings.from_env({**base, "TCC_WEBHOOK_URLS": URL, "TCC_WEBHOOK_SECRET": SECRET,
                           "TCC_WEBHOOK_TOKEN": WTOKEN})  # fmt: skip
    assert (s.webhook_urls, s.webhook_secret, s.webhook_token) == ((URL,), SECRET, WTOKEN)
    s = Settings.from_env(
        {**base, "TCC_WEBHOOK_URLS": f"{URL}, {URL2}", "TCC_WEBHOOK_SECRET": SECRET}
    )
    assert (s.webhook_urls, s.webhook_token) == ((URL, URL2), "")
    # a secret or token without URLs is ignored
    s = Settings.from_env({**base, "TCC_WEBHOOK_SECRET": "short", "TCC_WEBHOOK_TOKEN": "short"})
    assert (s.webhook_urls, s.webhook_secret, s.webhook_token) == ((), "", "")
    with pytest.raises(ValueError, match="TCC_WEBHOOK_SECRET is not set"):
        Settings.from_env({**base, "TCC_WEBHOOK_URLS": URL})
    with pytest.raises(ValueError, match="TCC_WEBHOOK_SECRET must be at least 16") as e:
        Settings.from_env({**base, "TCC_WEBHOOK_URLS": URL, "TCC_WEBHOOK_SECRET": "short"})
    assert "short" not in str(e.value)
    for bad in ("short", "t\u00e9st-webhook-token-01"):
        with pytest.raises(ValueError, match="TCC_WEBHOOK_TOKEN must be at least 16") as e:
            Settings.from_env({**base, "TCC_WEBHOOK_URLS": URL, "TCC_WEBHOOK_SECRET": SECRET,
                               "TCC_WEBHOOK_TOKEN": bad})  # fmt: skip
        assert bad not in str(e.value)
    with pytest.raises(ValueError, match="TCC_WEBHOOK_URLS entry 1") as e:
        Settings.from_env({**base, "TCC_WEBHOOK_URLS": "https://hooks.example.invalid:99999/x",
                           "TCC_WEBHOOK_SECRET": SECRET})  # fmt: skip
    assert "99999" not in str(e.value)


HOOKS = {"webhook_urls": (URL,), "webhook_secret": SECRET}


async def test_build_adds_a_poster_only_when_webhooks_are_set(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock)
    assert app.webhooks is None and app.rt._webhooks is None and app.bot._webhook_test is None
    app = make_app(tmp_path, tg, clock, webhook_token=WTOKEN, **HOOKS)
    assert isinstance(app.webhooks, WebhookPoster) and app.rt._webhooks is app.webhooks
    assert app.webhooks.urls == (URL,) and app.webhooks._token == WTOKEN
    assert app.bot._webhook_test is not None


def stored_copies(tmp_path: Path) -> tuple:
    return JsonFileStore(tmp_path / "tcc-state.json").load().webhook_copies


async def warn(app: App, n: int) -> None:
    """A loud application warning, delivered by the watchdog pass, the webhook pass awaited."""
    clock = app.rt._clock
    app.rt._app_warning(EventKind.KEY_REJECTED, f"alert {n}.", clock.now(), str(n))
    await app.rt.watchdog_once()
    await app.rt.webhooks_idle()


async def test_disabled_webhooks_drop_saved_copies(tmp_path, tg):  # Review Focus 13
    clock = TickClock(at("07:50"))
    rx = Receiver(SECRET, lambda: clock.now().timestamp(), status=503)
    tg.other = rx.handler
    app = make_app(tmp_path, tg, clock, **HOOKS)
    await app.rt.start()
    await warn(app, 1)
    owed = {c.id for c in stored_copies(tmp_path)}
    assert rx.requests and len(owed) >= 2  # the start's alert and alert 1
    phone = [m["text"] for m in tg.sent]
    assert "App: alert 1." in phone
    tried = len(rx.requests)
    app = make_app(tmp_path, tg, clock)  # restarted without TCC_WEBHOOK_URLS
    await app.rt.start()
    await warn(app, 2)
    assert stored_copies(tmp_path) == ()
    assert [m["text"] for m in tg.sent] == [*phone, "App: alert 2."]  # Telegram as before
    rx.status = 200
    app = make_app(tmp_path, tg, clock, **HOOKS)
    await app.rt.start()
    clock.jump(2)
    await app.rt.watchdog_once()
    await app.rt.webhooks_idle()
    assert len(rx.requests) == tried  # nothing posted after webhooks are on again
    assert stored_copies(tmp_path) == ()

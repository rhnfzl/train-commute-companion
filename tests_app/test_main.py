import asyncio
import logging
import socket
from datetime import time
from pathlib import Path

import pytest
from builders import ONE_TRANSFER
from conftest import CHAT, TOKEN, FakeTelegram, StationsProvider, TickClock
from tcc_app.main import App, Settings, build, main, serve, setup_logging
from test_runtime import PROFILE, Provider, at

ONE_PROFILE = PROFILE.split('  - id: "p2"')[0].replace("NAME", "office_a")
CODES = frozenset({"AAA", "UT", "RTD"})
ENV = {"NS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": TOKEN}


def make_app(tmp_path: Path, tg: FakeTelegram, clock: TickClock, provider=None,
             lookahead_at: time = time(6, 0)) -> App:  # fmt: skip
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(ONE_PROFILE)
    s = Settings("k", TOKEN, CHAT, None, profiles, tmp_path, lookahead_at)
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

import asyncio
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from custom_components.train_commute_companion.host import Parts, RuntimeHost
from train_commute_companion.calendar import TZ
from train_commute_companion.lifecycle import NotTravelling
from train_commute_companion.replay import MemoryStore, PrintNotifier, ReplayProvider
from train_commute_companion.runtime import Runtime

PROFILES = Path(__file__).resolve().parents[1] / "tests" / "data" / "profiles"


class YieldingClock:
    """A fixed instant whose sleep yields for real, so `run_forever` lets the loop run."""

    def __init__(self) -> None:
        self.t = datetime(2026, 10, 27, 7, 50, tzinfo=TZ)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0.01)


class Crash(BaseException):
    """Not an Exception, so the runtime's per-step guard does not catch it."""


def builder(tmp_path: Path, *, crash: bool = False, closed: list[bool] | None = None):
    path = tmp_path / "profiles.yaml"
    shutil.copy(PROFILES / "tue_wed_and_thu.yaml", path)

    def build() -> Parts:
        clock = YieldingClock()
        provider = ReplayProvider(clock, [])
        provider.positions = {}
        rt = Runtime(
            profile_path=path,
            store=MemoryStore(),
            provider=provider,
            notifier=PrintNotifier(),
            clock=clock,
            known_stations=None,
        )
        if crash:

            async def boom() -> None:
                raise Crash

            rt.poll_once = boom

        async def close() -> None:
            if closed is not None:
                closed.append(True)

        return Parts(rt, clock, provider, close)

    return build


def no_runtime_thread() -> bool:
    return not any(t.name == "tcc-runtime" for t in threading.enumerate())


@pytest.fixture
async def host(hass, tmp_path):
    h = RuntimeHost(builder(tmp_path), autorun=True)
    await h.start()
    yield h
    await h.stop()


async def test_run_executes_on_the_runtime_thread(hass, host):
    assert await host.run(lambda rt: threading.current_thread().name) == "tcc-runtime"


async def test_run_awaits_coroutines_and_propagates_errors(hass, host):
    assert await host.run(lambda rt: rt.act("nobody", NotTravelling(None, False))) is False
    with pytest.raises(ZeroDivisionError):
        await host.run(lambda rt: 1 / 0)


async def test_positions_is_a_copy_read_on_the_runtime_thread(hass, host):
    assert await host.positions() == {}


async def test_stop_cancels_run_forever_and_joins(hass, tmp_path, caplog):
    closed: list[bool] = []
    host = RuntimeHost(builder(tmp_path, closed=closed), autorun=True)
    await host.start()
    assert host.alive
    t0 = time.monotonic()
    await host.stop()
    # the drain resolves before the loop stops: no JOIN_TIMEOUT_S wait, no ERROR on each reload
    assert time.monotonic() - t0 < 5
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    await host.stop()
    assert not host.alive
    assert closed == [True]
    assert no_runtime_thread()


async def test_real_crash_then_stop_leaves_no_task_or_thread(hass, tmp_path):
    closed: list[bool] = []
    host = RuntimeHost(builder(tmp_path, crash=True, closed=closed), autorun=True)
    await host.start()
    for _ in range(500):
        if not host.alive:
            break
        await asyncio.sleep(0.01)
    assert not host.alive
    pending = await host.run(
        lambda rt: [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    )
    assert pending  # the watchdog outlived run_forever
    await host.stop()
    assert all(t.done() for t in pending)
    assert closed == [True]
    assert no_runtime_thread()


async def test_build_error_surfaces_from_start(hass):
    def build() -> Parts:
        raise ValueError("bad build")

    host = RuntimeHost(build, autorun=True)
    with pytest.raises(ValueError, match="bad build"):
        await host.start()
    await host.stop()
    assert no_runtime_thread()

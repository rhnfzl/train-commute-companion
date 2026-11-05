import asyncio
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from custom_components.train_commute_companion.host import Parts, RuntimeHost, RuntimeNotRunning
from train_commute_companion.calendar import TZ
from train_commute_companion.model import NotTravelling
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
        rt = Runtime(
            profile_path=path,
            store=MemoryStore(),
            provider=provider,
            notifier=PrintNotifier(),
            clock=clock,
            require_stations=False,
        )
        if crash:

            async def boom() -> None:
                raise Crash

            rt.poll_once = boom

        async def close() -> None:
            if closed is not None:
                closed.append(True)

        return Parts(rt, clock, close)

    return build


def no_runtime_thread() -> bool:
    return not any(t.name == "tcc-runtime" for t in threading.enumerate())


@pytest.fixture
async def host(hass, tmp_path):
    h = RuntimeHost(builder(tmp_path), retired=threading.Event(), autorun=True)
    await h.start()
    yield h
    await h.stop()


async def test_run_executes_on_the_runtime_thread(hass, host):
    assert await host.run(lambda rt: threading.current_thread().name) == "tcc-runtime"


async def test_run_awaits_coroutines_and_propagates_errors(hass, host):
    assert await host.run(lambda rt: rt.act("nobody", NotTravelling(None, False))) is not None
    with pytest.raises(ZeroDivisionError):
        await host.run(lambda rt: 1 / 0)


async def test_stop_cancels_run_forever_and_joins(hass, tmp_path, caplog):
    closed: list[bool] = []
    host = RuntimeHost(builder(tmp_path, closed=closed), retired=threading.Event(), autorun=True)
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
    host = RuntimeHost(
        builder(tmp_path, crash=True, closed=closed), retired=threading.Event(), autorun=True
    )
    await host.start()
    for _ in range(500):
        if not host.alive:
            break
        await asyncio.sleep(0.01)
    assert not host.alive
    pending = await host.run(
        lambda rt: [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    )
    assert not pending  # run_forever took its watchdog with it
    await host.stop()
    assert closed == [True]
    assert no_runtime_thread()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
async def test_run_after_the_loop_crashed_finds_no_runtime(hass, tmp_path):
    host = RuntimeHost(builder(tmp_path), retired=threading.Event(), autorun=False)
    await host.start()

    def crash() -> None:
        raise SystemExit  # the one kind of error a loop lets out of run_forever

    host._loop.call_soon_threadsafe(crash)
    await asyncio.to_thread(host._thread.join, 10)
    assert host._loop.is_closed()
    with pytest.raises(RuntimeNotRunning):
        await host.run(lambda rt: None)
    await host.stop()


async def test_build_error_surfaces_from_start(hass):
    def build() -> Parts:
        raise ValueError("bad build")

    host = RuntimeHost(build, retired=threading.Event(), autorun=True)
    with pytest.raises(ValueError, match="bad build"):
        await host.start()
    await host.stop()
    assert no_runtime_thread()

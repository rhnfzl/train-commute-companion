"""The runtime on its own thread and event loop. Core objects are touched only there, through
`RuntimeHost.run`; the one cross-thread read is `RuntimeHost.alive`."""

import asyncio
import concurrent.futures
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx

from train_commute_companion.providers.base import Provider
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.runtime import Clock, Runtime, SystemClock

_LOGGER = logging.getLogger(__name__)

THREAD_NAME = "tcc-runtime"
JOIN_TIMEOUT_S = 10

AUTORUN = True


class PositionedProvider(Provider, Protocol):
    @property
    def positions(self) -> Mapping[str, tuple[float, float]]: ...


@dataclass
class Parts:
    runtime: Runtime
    clock: Clock
    provider: PositionedProvider
    close: Callable[[], Awaitable[None]]


def make_clock() -> Clock:
    return SystemClock()


def make_provider(
    api_key: str, clock: Clock
) -> tuple[PositionedProvider, Callable[[], Awaitable[None]]]:
    client = httpx.AsyncClient()
    return NsProvider(api_key, client, clock.now), client.aclose


def make_webhook_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _log_crash(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (e := task.exception()) is not None:
        _LOGGER.error("runtime stopped unexpectedly", exc_info=e)


def _serve(loop: asyncio.AbstractEventLoop) -> None:
    try:
        loop.run_forever()
    finally:
        # closed by its own thread, so a thread that outlives stop() still cleans up when it ends
        loop.close()


class RuntimeHost:
    def __init__(self, build: Callable[[], Parts], *, autorun: bool) -> None:
        self._build = build
        self._autorun = autorun
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._parts: Parts | None = None
        self._task: asyncio.Task[None] | None = None
        self._drain: concurrent.futures.Future[None] | None = None
        # set by stop(); the store and notifier built for this host refuse to write or deliver
        self.retired = threading.Event()

    async def start(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread = threading.Thread(target=_serve, args=(loop,), name=THREAD_NAME, daemon=True)
        self._thread.start()

        async def boot() -> None:
            # built on the runtime thread, so the provider's HTTP client binds to its loop
            self._parts = self._build()
            if self._autorun:
                self._task = asyncio.create_task(self._parts.runtime.run_forever())
                self._task.add_done_callback(_log_crash)

        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(boot(), loop))

    async def run[T](self, fn: Callable[[Runtime], Awaitable[T] | T]) -> T:
        loop, parts = self._loop, self._parts
        if loop is None or parts is None or self.retired.is_set():
            raise RuntimeError("the runtime is not running")

        async def call() -> T:
            r = fn(parts.runtime)
            return cast(T, await r) if inspect.isawaitable(r) else r

        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(call(), loop))

    @property
    def alive(self) -> bool:
        t, task = self._thread, self._task
        if t is None or not t.is_alive() or self.retired.is_set():
            return False
        return not self._autorun or (task is not None and not task.done())

    async def positions(self) -> dict[str, tuple[float, float]]:
        def read(_: Runtime) -> dict[str, tuple[float, float]]:
            assert self._parts is not None
            return dict(self._parts.provider.positions)

        return await self.run(read)

    async def stop(self) -> None:
        """Never raises a shutdown timeout: a runtime stuck in a synchronous call is retired (no
        writes, no deliveries) and keeps its references, so a later stop can still finish it."""
        self.retired.set()
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return

        async def drain() -> None:
            try:
                me = asyncio.current_task()
                tasks: list[asyncio.Future[Any]] = [t for t in asyncio.all_tasks() if t is not me]
                for t in tasks:
                    t.cancel()
                # a crashed run_forever leaves its siblings (the watchdog) running
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                try:
                    # read here, after boot: a stop that interrupted start still closes its parts
                    if self._parts is not None:
                        await self._parts.close()
                finally:
                    # deferred one turn: a direct stop() exits the loop before this task's done
                    # callback resolves the cross-thread future, so stop() always timed out
                    loop.call_soon(loop.stop)

        if thread.is_alive():
            if self._drain is None:
                self._drain = asyncio.run_coroutine_threadsafe(drain(), loop)
            drained = False
            try:
                # waited in a worker thread: wall time, whatever hass.loop's clock does
                await asyncio.to_thread(self._drain.result, JOIN_TIMEOUT_S)
                drained = True
            except TimeoutError:
                _LOGGER.error(
                    "the runtime did not stop within %s s; it is retired and can no longer "
                    "write state or send alerts",
                    JOIN_TIMEOUT_S,
                )
            finally:
                if drained or self._drain.done():
                    await asyncio.to_thread(thread.join, JOIN_TIMEOUT_S)
        if not thread.is_alive():
            self._loop = self._thread = None  # idempotent: a later stop finds nothing to do

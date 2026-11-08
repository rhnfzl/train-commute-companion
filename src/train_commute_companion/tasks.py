"""Task owners preserve shutdown and action outcomes when callers stop waiting."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

log = logging.getLogger(__name__)


class RefreshPhase(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    RERUN = "rerun"
    STOPPING = "stopping"
    STOPPED = "stopped"


class RefreshTask:
    def __init__(self, work: Callable[[], Awaitable[None]], *, coalesce: bool) -> None:
        self._work, self._coalesce = work, coalesce
        self.phase = RefreshPhase.IDLE
        self.task: asyncio.Task[None] | None = None

    def request(self) -> None:
        if self.phase in (RefreshPhase.STOPPING, RefreshPhase.STOPPED):
            return
        if self.phase in (RefreshPhase.RUNNING, RefreshPhase.RERUN):
            if self._coalesce:
                self.phase = RefreshPhase.RERUN
            return
        self.phase = RefreshPhase.RUNNING
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._observe)

    async def _run(self) -> None:
        try:
            while True:
                self.phase = RefreshPhase.RUNNING
                await self._work()
                if self.phase is not RefreshPhase.RERUN:
                    return
        finally:
            if self.phase not in (RefreshPhase.STOPPING, RefreshPhase.STOPPED):
                self.phase = RefreshPhase.IDLE

    @staticmethod
    def _observe(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("background refresh failed", exc_info=error)

    async def idle(self) -> None:
        while (task := self.task) is not None and not task.done():
            await asyncio.wait({task})

    async def stop(self) -> None:
        if self.phase is RefreshPhase.STOPPED:
            return
        task = self.task
        if self.phase is not RefreshPhase.STOPPING:
            self.phase = RefreshPhase.STOPPING
            if task is not None and not task.done():
                task.cancel()
        try:
            if task is not None and not task.done():
                await asyncio.wait({task})
        finally:
            if task is None or task.done():
                self.phase = RefreshPhase.STOPPED


class CallPhase(StrEnum):
    WAITING = "waiting"
    DETACHED = "detached"
    FINISHED = "finished"


class DetachedCall[T]:
    def __init__(
        self,
        task: asyncio.Future[T],
        on_detached_failure: Callable[[BaseException], None],
    ) -> None:
        self.task = task
        self.phase = CallPhase.WAITING
        self._report = on_detached_failure
        self._failure: BaseException | None = None
        self._reported = False
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Future[T]) -> None:
        if not task.cancelled():
            self._failure = task.exception()
        if self.phase is CallPhase.DETACHED:
            self._report_failure()
        self.phase = CallPhase.FINISHED

    def _report_failure(self) -> None:
        if self._failure is not None and not self._reported:
            self._reported = True
            self._report(self._failure)

    def _detach(self) -> None:
        if self.phase is CallPhase.FINISHED:
            self._report_failure()
        else:
            self.phase = CallPhase.DETACHED

    async def wait(self, timeout_seconds: float | None) -> T:
        timer = asyncio.timeout(timeout_seconds)
        try:
            async with timer:
                await asyncio.wait({self.task})
                return self.task.result()
        except TimeoutError:
            if timer.expired():
                self._detach()
            raise
        except asyncio.CancelledError:
            self._detach()
            raise

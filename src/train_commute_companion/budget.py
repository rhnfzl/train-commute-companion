"""The shared request budget: one per API key, across every profile and look-ahead (7.12 rule 14),
and the one way a provider request goes out under it."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from train_commute_companion.calendar import to_utc
from train_commute_companion.providers.base import (
    AuthError,
    MalformedAnswer,
    Provider,
    ProviderAnswer,
    ProviderError,
    Query,
    RateLimited,
    ServerError,
)
from train_commute_companion.state import PollFailure

log = logging.getLogger(__name__)

POLL_DEADLINE = timedelta(seconds=90)
POLL_EVERY_S = 120
BACKOFF_MIN = (2, 4, 8)
# a rejected key: stays at 30 until a success, never stops (the key may be fixed on NS's side)
BACKOFF_AUTH_MIN = (2, 5, 15, 30)


class BudgetPhase(StrEnum):
    OPEN = "open"
    COOLDOWN = "cooldown"
    LIVE_ONLY = "live_only"


@dataclass(frozen=True, slots=True)
class RequestBudget:
    cooldown_until: datetime | None = None
    backoff_step: int = 0
    reason: PollFailure = "rate"  # which table the step counts in; a skip reports it (7.11)

    def phase(self, now: datetime) -> BudgetPhase:
        if self.cooldown_until is None:
            return BudgetPhase.OPEN
        elapsed = to_utc(now) - to_utc(self.cooldown_until)
        if elapsed < timedelta():
            return BudgetPhase.COOLDOWN
        if elapsed < timedelta(seconds=POLL_EVERY_S):
            return BudgetPhase.LIVE_ONLY
        return BudgetPhase.OPEN

    def allow(self, now: datetime, live: bool) -> bool:
        phase = self.phase(now)
        return phase is BudgetPhase.OPEN or live and phase is BudgetPhase.LIVE_ONLY

    def on_result(
        self, error: BaseException | None, now: datetime, window_end: datetime | None
    ) -> "RequestBudget":
        if error is None:
            return RequestBudget()
        reason: PollFailure
        table: tuple[int, ...]
        if isinstance(error, AuthError):
            reason, table = "auth", BACKOFF_AUTH_MIN
        elif isinstance(error, (RateLimited, ServerError)):
            reason, table = "rate", BACKOFF_MIN
        else:
            return self
        step = min((self.backoff_step if self.reason == reason else 0) + 1, len(table))
        until = to_utc(now) + timedelta(minutes=table[step - 1])
        if window_end is not None and to_utc(now) < to_utc(window_end):
            until = min(until, to_utc(window_end))
        return RequestBudget(until, step, reason)


def _failure(e: ProviderError) -> PollFailure:
    if isinstance(e, AuthError):
        return "auth"
    if isinstance(e, RateLimited):
        return "rate"
    return "parse" if isinstance(e, MalformedAnswer) else "provider"


class RequestGate:
    """Every provider request: checked against the budget, counted, timed out, its failure named
    as a `PollFailure` and the budget updated. Each caller reports a rejected key itself: what
    counts as an outage differs (a poll judges all its requests as one)."""

    def __init__(self, provider: Provider, now: Callable[[], datetime]) -> None:
        self._provider, self._now = provider, now
        self.budget = RequestBudget()
        self.requests = 0  # NS publishes no quota (7.1): counted and logged

    async def _request[T](
        self,
        ask: Callable[[], Awaitable[T]],
        what: object,
        live: bool,
        window_end: datetime | None,
        deadline: datetime | None,
    ) -> T | PollFailure:
        """`deadline`: past it even an answer is a timeout. None: `POLL_DEADLINE` from now, and
        whatever came back in that time counts."""
        now = self._now()
        if not self.budget.allow(now, live):
            # skipped by the shared backoff: counts toward board health (7.1), and a rejected
            # key still reads as one through its cooldown (7.11)
            return self.budget.reason
        left = POLL_DEADLINE if deadline is None else to_utc(deadline) - to_utc(now)
        if left <= timedelta(0):
            return "timeout"
        before = self.budget
        self.requests += 1
        log.debug("request %d: %s", self.requests, what)
        try:
            answer = await asyncio.wait_for(ask(), left.total_seconds())
        except TimeoutError:
            return "timeout"  # neither backs off nor resets it
        except ProviderError as e:
            self.budget = self.budget.on_result(e, self._now(), window_end)
            return _failure(e)
        if deadline is not None and to_utc(self._now()) > to_utc(deadline):
            return "timeout"  # a late answer never overwrites a newer one (7.1)
        # a success clears only the state it started under: an older request finishing late
        # never clears a newer 429 cooldown
        if self.budget is before:
            self.budget = RequestBudget()
        return answer

    async def trips(
        self,
        q: Query,
        live: bool,
        window_end: datetime | None,
        deadline: datetime | None = None,
    ) -> ProviderAnswer | PollFailure:
        """`deadline`: the poll's own hard deadline (7.1), shared by all its requests."""
        deadline = deadline or to_utc(self._now()) + POLL_DEADLINE
        return await self._request(lambda: self._provider.trips(q), q, live, window_end, deadline)

    async def all_trips(
        self,
        queries: Sequence[Query],
        live: bool,
        window_end: datetime | None,
        deadline: datetime | None = None,
    ) -> tuple[ProviderAnswer, ...] | PollFailure:
        """All or nothing: a poll missing one answer cannot judge the legs it was for (rule 2).
        One hard deadline for the whole poll, not one per request (7.1)."""
        answers = []
        deadline = deadline or to_utc(self._now()) + POLL_DEADLINE
        for q in queries:
            a = await self.trips(q, live, window_end, deadline)
            if isinstance(a, str):
                return a
            answers.append(a)
        return tuple(answers)

    async def listing[T](
        self, ask: Callable[[], Awaitable[T]], what: str
    ) -> T | PollFailure | None:
        """A request outside any journey, behind the live ones; None when the budget refuses it."""
        if not self.budget.allow(self._now(), live=False):
            return None
        return await self._request(ask, what, False, None, None)

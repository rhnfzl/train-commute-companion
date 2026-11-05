"""The shared request budget: one per API key, across every profile and look-ahead (7.12 rule 14)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from train_commute_companion.calendar import to_utc
from train_commute_companion.providers.base import AuthError, RateLimited, ServerError
from train_commute_companion.state import PollFailure

POLL_EVERY_S = 120
BACKOFF_MIN = (2, 4, 8)
# a rejected key: stays at 30 until a success, never stops (the key may be fixed on NS's side)
BACKOFF_AUTH_MIN = (2, 5, 15, 30)


@dataclass(frozen=True, slots=True)
class RequestBudget:
    cooldown_until: datetime | None = None
    backoff_step: int = 0
    reason: PollFailure = "rate"  # which table the step counts in; a skip reports it (7.11)

    def allow(self, now: datetime, live: bool) -> bool:
        if self.cooldown_until is None:
            return True
        # live journeys first: look-ahead waits one more poll interval after a cooldown
        extra = timedelta(0) if live else timedelta(seconds=POLL_EVERY_S)
        return to_utc(now) >= to_utc(self.cooldown_until) + extra

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

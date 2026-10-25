"""Provider contract: neutral answer types, errors and the protocol."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from train_commute_companion.model import Connection, Stop


class ProviderError(Exception):
    pass


class AuthError(ProviderError):
    pass


class RateLimited(ProviderError):
    def __init__(self, message: str = "rate limited", retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(ProviderError):
    """The provider answered HTTP 5xx: retryable, counts toward shared backoff (7.1)."""


class MalformedAnswer(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class Query:
    origin: str
    destination: str
    at: datetime
    via: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderAnswer:
    query: Query
    connections: tuple[Connection, ...]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class PlannedWork:
    start: datetime
    end: datetime
    stations: frozenset[str]  # uic codes plus any station-code aliases
    title: str

    def lists(self, stop: Stop) -> bool:
        """True when the work lists the stop or cannot rule it out. Planned works only gate
        clean probes (7.10), so a stop without a uic, which a uic-only work entry could name,
        counts as listed: an unresolved identity never yields a clean probe."""
        if stop.code in self.stations:
            return True
        return stop.uic is None or stop.uic in self.stations


@dataclass(frozen=True, slots=True)
class PlannedWorks:
    items: tuple[PlannedWork, ...]
    coverage: Literal["complete", "unknown"]


class Provider(Protocol):
    async def trips(self, q: Query) -> ProviderAnswer: ...

    async def planned_works(self) -> PlannedWorks:
        """Raises ProviderError on a failed request, so 429/5xx feed shared backoff;
        an unreadable answer is coverage `unknown`."""
        ...

    async def stations(self) -> frozenset[str]: ...

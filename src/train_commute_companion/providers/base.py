"""Provider contract: neutral answer types, errors and the protocol."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from train_commute_companion.model import Connection


class ProviderError(Exception):
    pass


class AuthError(ProviderError):
    pass


class RateLimited(ProviderError):
    def __init__(self, message: str = "rate limited", retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
    stations: frozenset[str]
    title: str


@dataclass(frozen=True, slots=True)
class PlannedWorks:
    items: tuple[PlannedWork, ...]
    coverage: Literal["complete", "unknown"]


class Provider(Protocol):
    async def trips(self, q: Query) -> ProviderAnswer: ...

    async def planned_works(self) -> PlannedWorks: ...

    async def stations(self) -> frozenset[str]: ...

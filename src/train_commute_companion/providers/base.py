"""Provider contract: neutral answer types, errors and the protocol."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, Self

from train_commute_companion.model import Connection, Profile, Stop


class ProviderError(Exception):
    pass


class AuthError(ProviderError):
    pass


class DoorToDoorNotAllowed(ProviderError):
    """The API key cannot request journeys from or to a non-station location."""


class RateLimited(ProviderError):
    def __init__(self, message: str = "rate limited", retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(ProviderError):
    """The provider answered HTTP 5xx: retryable, counts toward shared backoff (7.1)."""


class MalformedAnswer(ProviderError):
    pass


@dataclass(frozen=True, slots=True, eq=False)
class Query:
    origin: str
    destination: str
    at: datetime
    via: str | None = None
    require_accessible: bool = False
    search_for_arrival: bool = False  # at is the destination time when true

    def _key(self) -> tuple[str, str, datetime, str | None, bool, bool]:
        return (
            self.origin,
            self.destination,
            self.at.astimezone(UTC),
            self.via,
            self.require_accessible,
            self.search_for_arrival,
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Query) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    @classmethod
    def for_profile(
        cls,
        profile: Profile,
        origin: str,
        destination: str,
        at: datetime,
        via: str | None = None,
        *,
        search_for_arrival: bool = False,
    ) -> Self:
        return cls(
            origin,
            destination,
            at,
            via,
            profile.accessibility.require_accessible,
            search_for_arrival,
        )


@dataclass(frozen=True, slots=True)
class ProviderAnswer:
    query: Query
    connections: tuple[Connection, ...]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class Pool(ProviderAnswer):
    """All answers of one poll merged for ranking, plus each answer apart: per-leg evidence
    keeps the answer and the time it was observed in (rule 6, rule 20)."""

    answers: tuple[ProviderAnswer, ...] = ()


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


@dataclass(frozen=True, slots=True)
class DisruptionSection:
    stations: frozenset[str]
    # "SPR" or "IC" when NS names exactly that one train type as hit here; None when unknown
    only: str | None = None
    size: int = 0  # how many stations NS names; 0 in notices stored before it was counted


@dataclass(frozen=True, slots=True)
class ActiveDisruption:
    id: str
    title: str
    stations: frozenset[str]
    url: str | None
    # Persisted inside notices: new fields need defaults so older state files still load.
    summary: str = ""  # NS situation label: cause and effect
    end: datetime | None = None  # NS expected end
    sections: tuple[DisruptionSection, ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveDisruptions:
    items: tuple[ActiveDisruption, ...]
    coverage: Literal["complete", "unknown"]


class Provider(Protocol):
    @property
    def positions(self) -> Mapping[str, tuple[float, float]]:
        """Station code -> (latitude, longitude); empty until `stations` has succeeded."""
        ...

    async def trips(self, q: Query) -> ProviderAnswer:
        """Raises ProviderError or a subclass on a failed request or an unreadable answer, a
        transport error included: the runtime handles nothing else from a provider but its own
        deadline's TimeoutError."""
        ...

    async def planned_works(self) -> PlannedWorks:
        """Raises ProviderError on a failed request, so 429/5xx feed shared backoff;
        an unreadable answer is coverage `unknown`."""
        ...

    async def active_disruptions(self) -> ActiveDisruptions:
        """The active feed. Unknown coverage must never establish an incident has cleared."""
        ...

    async def stations(self) -> frozenset[str]:
        """Raises as `trips` does."""
        ...

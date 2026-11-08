"""Own runtime startup, profile reads and authentication episodes independently."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from train_commute_companion.calendar import to_utc
from train_commute_companion.model import OccurrenceKey
from train_commute_companion.state import OccurrencePhase, OccurrenceState, PollFailure


class ResultPhase(StrEnum):
    CURRENT = "current"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class OccurrenceRequest:
    key: OccurrenceKey
    generation: int
    route_revision: int
    baseline_version: int | None

    @classmethod
    def capture(cls, state: OccurrenceState) -> "OccurrenceRequest":
        return cls(state.key, state.generation, state.route_revision, state.baseline_version)

    def phase_for(self, state: OccurrenceState) -> ResultPhase:
        return ResultPhase.CURRENT if self == self.capture(state) else ResultPhase.STALE


class RouteDecision(StrEnum):
    ADOPT = "adopt"
    RETAIN = "retain"
    RECREATE = "recreate"


def route_decision(state: OccurrenceState, same_route: bool) -> RouteDecision:
    if same_route:
        return RouteDecision.ADOPT
    if (
        state.lock is not None
        or state.retained
        or state.phase in (OccurrencePhase.RESOLVED, OccurrencePhase.SKIPPED)
        or (state.selected_recovery is not None and state.selected_recovery.status == "active")
    ):
        return RouteDecision.RETAIN
    return RouteDecision.RECREATE


class RuntimePhase(StrEnum):
    NEW = "new"
    RECONCILING = "reconciling"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RunnerPhase(StrEnum):
    AVAILABLE = "available"
    OWNED = "owned"
    RELEASED = "released"


@dataclass(slots=True)
class RuntimeLifecycle:
    phase: RuntimePhase = RuntimePhase.NEW
    reconcile_started_at: datetime | None = None
    runner_phase: RunnerPhase = RunnerPhase.AVAILABLE
    _runner: object | None = field(default=None, repr=False)
    _initializer: object | None = field(default=None, repr=False)

    def claim_runner(self, started: bool, owner: object) -> None:
        expected = RuntimePhase.RUNNING if started else RuntimePhase.NEW
        if self.runner_phase is not RunnerPhase.AVAILABLE or self.phase is not expected:
            raise RuntimeError("the runtime already has an owner or cannot start in this state")
        self.runner_phase = RunnerPhase.OWNED
        self._runner = owner

    def initialize(self, owner: object, initializer: object) -> None:
        if self.runner_phase is not RunnerPhase.OWNED or self._runner is not owner:
            raise RuntimeError("only the runtime owner can initialize it")
        self._initializer = initializer

    def release_runner(self, owner: object) -> None:
        if self.runner_phase is not RunnerPhase.OWNED or self._runner is not owner:
            raise RuntimeError("only the runtime owner can release it")
        self.runner_phase = RunnerPhase.RELEASED
        self._runner = self._initializer = None

    def begin(self, now: datetime, initializer: object | None = None) -> None:
        if self.phase is not RuntimePhase.NEW or (
            self.runner_phase is RunnerPhase.OWNED and self._initializer is not initializer
        ):
            raise RuntimeError("a runtime can start only once; create a new owner")
        self.phase = RuntimePhase.RECONCILING
        self.reconcile_started_at = now

    def reconciled(self) -> None:
        if self.phase is not RuntimePhase.RECONCILING:
            raise RuntimeError("the runtime is not reconciling")
        self.reconcile_started_at = None
        self.phase = RuntimePhase.STARTING

    def running(self) -> None:
        if self.phase is not RuntimePhase.STARTING:
            raise RuntimeError("the runtime has not reconciled")
        self.phase = RuntimePhase.RUNNING

    def holds(self, created_at: datetime) -> bool:
        return (
            self.phase is RuntimePhase.RECONCILING
            and self.reconcile_started_at is not None
            and to_utc(created_at) < to_utc(self.reconcile_started_at)
        )


class ReloadPhase(StrEnum):
    UNCONFIGURED = "unconfigured"
    CHECKING = "checking"
    READY = "ready"
    WAITING_STATIONS = "waiting_stations"
    REFUSED = "refused"


class PendingEditPhase(StrEnum):
    NONE = "none"
    STATIONS = "stations"


@dataclass(frozen=True, slots=True)
class ProfileRead:
    sequence: int
    pending: PendingEditPhase


@dataclass(slots=True)
class ReloadControl:
    phase: ReloadPhase = ReloadPhase.UNCONFIGURED
    refusal: str | None = None
    announced: str | None = None
    _sequence: int = 0
    pending: PendingEditPhase = PendingEditPhase.NONE

    def begin(self) -> ProfileRead:
        request = ProfileRead(self._sequence + 1, self.pending)
        self._sequence = request.sequence
        self.phase = ReloadPhase.CHECKING
        return request

    def current(self, request: ProfileRead) -> bool:
        return request.sequence == self._sequence

    def reject(self, reason: str, *, waiting_stations: bool = False) -> None:
        self.refusal = reason
        self.phase = ReloadPhase.WAITING_STATIONS if waiting_stations else ReloadPhase.REFUSED
        if waiting_stations:
            self.pending = PendingEditPhase.STATIONS


class AuthPhase(StrEnum):
    HEALTHY = "healthy"
    REJECTED = "rejected"


@dataclass(slots=True)
class AuthEpisode:
    phase: AuthPhase = AuthPhase.HEALTHY
    told: set[str] = field(default_factory=set)

    def observe(self, error: PollFailure | None, who: str) -> bool:
        if error is None:
            self.phase = AuthPhase.HEALTHY
            self.told.clear()
            return False
        if error != "auth" or (self.phase is AuthPhase.REJECTED and who in self.told):
            return False
        self.phase = AuthPhase.REJECTED
        self.told.add(who)
        return True

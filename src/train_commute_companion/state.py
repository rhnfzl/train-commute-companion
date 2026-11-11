"""The persisted records of a followed occurrence and of the look-ahead: what the store keeps,
with only the reads that belong to the data. The rules that change them live elsewhere."""

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal, NamedTuple

from train_commute_companion.calendar import to_utc
from train_commute_companion.compensation_state import PlannedJourney
from train_commute_companion.model import (
    FOLLOW_LIMIT,
    AdviceRecord,
    DirectionState,
    EventKind,
    LegState,
    Obligation,
    OccurrenceKey,
    Severity,
    Signature,
    TrainId,
    present,
    train_ident,
)
from train_commute_companion.providers.base import ActiveDisruption
from train_commute_companion.recovery_state import SelectedRecovery

PollFailure = Literal["auth", "rate", "provider", "parse", "timeout"]
ACCEPT_AFTER = timedelta(minutes=5)
ACCEPT_APART = timedelta(minutes=4)


@dataclass(frozen=True, slots=True)
class PollRecord:
    at: datetime
    ok: bool
    error: PollFailure | None


class LockSource(StrEnum):
    AUTO = "auto"
    LOCATION = "location"
    STATION = "station"
    CHOOSE = "choose"
    MISSED = "missed"
    TIMEOUT = "timeout"
    RELOCK = "relock"
    RESTORE = "restore"


class Commitment(StrEnum):
    INTENDED = "intended"
    PROVISIONAL = "provisional"
    COMMITTED = "committed"


class ArrivalPhase(StrEnum):
    UNSEEN = "unseen"
    STABILIZING = "stabilizing"
    ACCEPTED = "accepted"
    WATCHING = "watching"
    INACTIVE = "inactive"


class EndpointPhase(StrEnum):
    MOVABLE = "movable"
    FROZEN = "frozen"


class OccurrencePhase(StrEnum):
    FOLLOWING = "following"
    RESOLVED = "resolved"
    SKIPPED = "skipped"


class ArchivePhase(StrEnum):
    FOLLOWING = "following"
    RECOVERY_PENDING = "recovery_pending"
    WARNING_PENDING = "warning_pending"
    WATCHING = "watching"
    READY = "ready"


class BaselineBinding(StrEnum):
    UNHYDRATED = "unhydrated"
    EMPTY = "empty"
    SIGNATURE = "signature"


class BaselinePhase(StrEnum):
    PROVISIONAL_FUTURE = "provisional_future"
    PROVISIONAL_CURRENT = "provisional_current"
    CONFIRMED_FUTURE = "confirmed_future"
    CONFIRMED_CURRENT = "confirmed_current"

    @property
    def confirmed(self) -> bool:
        return self in (BaselinePhase.CONFIRMED_FUTURE, BaselinePhase.CONFIRMED_CURRENT)

    @property
    def current(self) -> bool:
        return self in (BaselinePhase.PROVISIONAL_CURRENT, BaselinePhase.CONFIRMED_CURRENT)


@dataclass(frozen=True, slots=True)
class LegProgress:
    train: TrainId
    board_station: str
    intended_alight: str
    served_endpoint: str
    alight_stop: str
    alight_frozen: bool
    state: LegState
    accepted_at: datetime | None
    accept_value: datetime | None
    accept_first_seen: datetime | None
    watch_until: datetime | None
    # expected arrival + transfer minimum at alight_stop, last observed: the earliest onward
    # departure, so a broken change is searched from then on (7.4). Persisted state: a store
    # that drops it falls back to searching from the planned departure.
    onward_from: datetime | None = None

    @property
    def arrival_phase(self) -> ArrivalPhase:
        if self.state in (LegState.CANCELLED, LegState.RELOCKED):
            return ArrivalPhase.INACTIVE
        if self.state is LegState.DONE:
            return ArrivalPhase.WATCHING if self.watch_until is not None else ArrivalPhase.ACCEPTED
        if self.accept_value is not None and self.accept_first_seen is not None:
            return ArrivalPhase.STABILIZING
        return ArrivalPhase.UNSEEN

    @property
    def endpoint_phase(self) -> EndpointPhase:
        return EndpointPhase.FROZEN if self.alight_frozen else EndpointPhase.MOVABLE

    def accept_arrival(self, at: datetime, *, observed_at: datetime | None = None) -> "LegProgress":
        if self.arrival_phase is not ArrivalPhase.STABILIZING:
            raise ValueError("arrival requires a stable candidate before acceptance")
        seen = to_utc(at if observed_at is None else observed_at)
        first, value = to_utc(present(self.accept_first_seen)), to_utc(present(self.accept_value))
        if seen - first < ACCEPT_APART or first < value + ACCEPT_AFTER:
            raise ValueError("arrival candidate is not stable long enough")
        return replace(self, state=LegState.DONE, accepted_at=at)

    def move_endpoint(self, served_endpoint: str, alight_stop: str) -> "LegProgress":
        if self.endpoint_phase is EndpointPhase.FROZEN:
            alight_stop = self.alight_stop
        moved = replace(self, served_endpoint=served_endpoint, alight_stop=alight_stop)
        if alight_stop == self.alight_stop or self.state is not LegState.DONE:
            return moved
        return replace(
            moved,
            state=LegState.RIDING,
            accepted_at=None,
            accept_value=None,
            accept_first_seen=None,
            watch_until=None,
        )

    def observed_at(self, now: datetime) -> bool:
        phase = self.arrival_phase
        if phase is ArrivalPhase.INACTIVE:
            return False
        return phase is not ArrivalPhase.WATCHING or to_utc(now) < to_utc(present(self.watch_until))


@dataclass(frozen=True, slots=True)
class Journey:
    legs: tuple[LegProgress, ...]
    source: LockSource
    commitment: Commitment
    planned_final_arrival: datetime
    plan: PlannedJourney | None = None


@dataclass(frozen=True, slots=True)
class DisruptionReceipt:
    """The source revisions one destination actually accepted."""

    start_revision: int = 0
    clear_revision: int = 0
    notified_revision: int = 0

    @property
    def needs_clear(self) -> bool:
        return self.start_revision > self.clear_revision


@dataclass(frozen=True, slots=True)
class DisruptionNotice:
    item: ActiveDisruption
    active: bool
    revision: int
    notified: bool = False
    relevant: bool = True
    announced: bool = False
    instance: str = ""
    audience: frozenset[str] | None = None
    receipts: Mapping[str, DisruptionReceipt] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OccurrenceState:
    key: OccurrenceKey
    generation: int
    route_revision: int
    settings_revision: int
    baseline_version: int | None
    direction_state: DirectionState
    lock: Journey | None
    retained: tuple[Journey, ...]
    travelling: bool
    tap_at: datetime | None
    at_station: bool
    errand_deadline: datetime | None
    not_travelling: bool
    cancellation_history: tuple[LegProgress, ...]  # trains replaced by a break, restorable (rule 8)
    missing_sent: bool
    all_good_evaluated: bool
    alert_keys: Mapping[str, str] = field(default_factory=dict)
    advice: AdviceRecord | None = None
    poll_records: tuple[PollRecord, ...] = ()
    # latest non-stale location time per zone: arrival evidence for a silent end (rule 6), and
    # what the news ledger reads to stop repeat silent messages. Persisted state: a store that
    # drops it turns a silent end after a restart into a loud stopped_following.
    zone_reports: Mapping[str, datetime] = field(default_factory=dict)
    # the baseline signature this occurrence started on (rule 10): a later re-seed of the
    # direction's history must not move a locked occurrence's schedule
    baseline_signature: Signature | None = None
    # True once `baseline_signature` is this occurrence's own (None then means it started with
    # no baseline); False only in stores written before it existed, hydrated at start
    baseline_frozen: bool = False
    disruptions: Mapping[str, DisruptionNotice] = field(default_factory=dict)
    disruptions_checked_at: datetime | None = None
    disruptions_coverage: Literal["complete", "unknown"] = "unknown"
    selected_recovery: SelectedRecovery | None = None
    original_plan: PlannedJourney | None = None
    skipped_direction_state: DirectionState | None = None

    @property
    def phase(self) -> OccurrencePhase:
        if self.not_travelling:
            return OccurrencePhase.SKIPPED
        if (
            self.direction_state in (DirectionState.DONE, DirectionState.EXPIRED)
            and not self.retained
        ):
            return OccurrencePhase.RESOLVED
        return OccurrencePhase.FOLLOWING

    @property
    def baseline_binding(self) -> BaselineBinding:
        if not self.baseline_frozen:
            return BaselineBinding.UNHYDRATED
        return (
            BaselineBinding.EMPTY if self.baseline_signature is None else BaselineBinding.SIGNATURE
        )

    def provisional_expired(self, now: datetime) -> bool:
        return (
            self.lock is not None
            and self.lock.commitment is Commitment.PROVISIONAL
            and self.errand_deadline is not None
            and to_utc(now) >= to_utc(self.errand_deadline)
        )

    def archive_phase(self, now: datetime, pending_journey_warnings: int) -> ArchivePhase:
        if self.selected_recovery is not None and self.selected_recovery.status == "active":
            return ArchivePhase.RECOVERY_PENDING
        if pending_journey_warnings > 0:
            return ArchivePhase.WARNING_PENDING
        if self.phase is OccurrencePhase.SKIPPED:
            return ArchivePhase.READY
        if self.direction_state is not DirectionState.DONE or self.retained:
            return ArchivePhase.FOLLOWING
        if self.lock is None:
            return ArchivePhase.READY
        watch = self.lock.legs[-1].watch_until
        limit = to_utc(self.lock.planned_final_arrival) + FOLLOW_LIMIT
        if watch is not None and to_utc(now) < min(to_utc(watch), limit):
            return ArchivePhase.WATCHING
        return ArchivePhase.READY


# `OccurrenceState.alert_keys` holds these entries in one stored mapping, read through the functions
# below: "sent:<fact key>" -> the severity it was sent at (the delivery ledger);
# "live:<fact key>" -> "1" for what the latest healthy poll reported: a fact that a healthy poll no
# longer reports has ended, so its delivery entry goes too and the same fact coming back later is
# news (7.5); "unclear#<train key>" -> the fingerprint last told (rule 23); and the slots named by
# `ToldDelay.slot` and `AlertFacts.moved_slot` -> the value last stated (`lifecycle.keep_stated`).
_LIVE_PREFIX = "live:"
_SENT_PREFIX = "sent:"


def sent_facts(keys: Mapping[str, str]) -> dict[str, Severity]:
    n = len(_SENT_PREFIX)
    return {k[n:]: Severity(v) for k, v in keys.items() if k.startswith(_SENT_PREFIX)}


def is_sent(keys: Mapping[str, str], fact: str) -> bool:
    return _SENT_PREFIX + fact in keys


def mark_sent(keys: MutableMapping[str, str], fact: str, severity: Severity) -> None:
    keys[_SENT_PREFIX + fact] = severity.value


def live_facts(keys: Mapping[str, str]) -> list[str]:
    return [k[len(_LIVE_PREFIX) :] for k in keys if k.startswith(_LIVE_PREFIX)]


def holds_live(
    keys: Mapping[str, str], kinds: Iterable[EventKind], train: TrainId | None = None
) -> bool:
    """A live fact of one of `kinds` is held; with `train`, one about that train."""
    heads = tuple(f"{k.value}:" for k in kinds)
    ident = "" if train is None else f":{train_ident(train)}:"
    return any(f.startswith(heads) and ident in f for f in live_facts(keys))


def mark_live(keys: MutableMapping[str, str], facts: Iterable[str]) -> None:
    keys.update({_LIVE_PREFIX + f: "1" for f in facts})


def mark_sent_live(keys: MutableMapping[str, str]) -> None:
    """Every delivered fact counts as last reported, without judging its stored severity."""
    n = len(_SENT_PREFIX)
    for k in list(keys):
        if k.startswith(_SENT_PREFIX):
            keys.setdefault(_LIVE_PREFIX + k[n:], "1")


def forget_fact(keys: MutableMapping[str, str], fact: str) -> None:
    """A live fact ended: it leaves the ledger, so its recurrence is news again (7.5)."""
    keys.pop(_LIVE_PREFIX + fact, None)
    keys.pop(_SENT_PREFIX + fact, None)


def unclear_slot(train_key: str) -> str:
    return f"unclear#{train_key}"


@dataclass(frozen=True, slots=True)
class BaselineVersion:
    version: int
    signature: Signature
    effective_from: date
    confirmed: bool
    commute_weekday: int | None = None

    def phase_on(self, day: date) -> BaselinePhase:
        if self.confirmed:
            return (
                BaselinePhase.CONFIRMED_FUTURE
                if self.effective_from > day
                else BaselinePhase.CONFIRMED_CURRENT
            )
        return (
            BaselinePhase.PROVISIONAL_FUTURE
            if self.effective_from > day
            else BaselinePhase.PROVISIONAL_CURRENT
        )

    def confirm(self) -> "BaselineVersion":
        return replace(self, confirmed=True)


@dataclass(frozen=True, slots=True)
class BaselineHistory:
    versions: tuple[BaselineVersion, ...] = ()
    floor: int = 0  # highest number retired (re-seed or cancellation): numbers never repeat

    def in_force(self, day: date, commute_weekday: int | None = None) -> BaselineVersion | None:
        live = [
            v
            for v in self.versions
            if v.phase_on(day).current and v.commute_weekday == commute_weekday
        ]
        return max(live, key=lambda v: (v.effective_from, v.version), default=None)

    @property
    def latest(self) -> int | None:
        return max((v.version for v in self.versions), default=None)

    def _next(self) -> int:
        return max(self.latest or 0, self.floor) + 1


class LookAheadFact(NamedTuple):
    """One statement of a look-ahead message, stored as the list [key, text, loud, warning]."""

    key: str
    text: str
    loud: bool
    warning: bool  # the message then closes with the replacement hint

    @property
    def adopts(self) -> bool:
        return self.key.startswith("adopt:")

    @property
    def cancels(self) -> bool:
        return self.key.startswith("cancel:")


@dataclass(frozen=True, slots=True)
class LookAheadLedger:
    # "<service date>|<leg index or j>" -> entries; a warn/loud/restored mark ends in
    # "#<id of the obligation that carries it>", read with lookahead_probe.entry_mark/entry_owner
    per_date: Mapping[str, tuple[str, ...]]
    pending: tuple[Obligation, ...]
    last_run: datetime | None  # last successful run (look-ahead health only)
    started_revision: int
    started_baseline: int | None
    last_applied: datetime | None = None  # start time of the last run whose writes were applied
    # pending obligation id -> its facts, so one fact can be withdrawn from a message that
    # bundles several (7.12 rule 18)
    facts: Mapping[str, tuple[LookAheadFact, ...]] = field(default_factory=dict)

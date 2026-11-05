"""The persisted records of a followed occurrence and of the look-ahead: what the store keeps,
with only the reads that belong to the data. The rules that change them live elsewhere."""

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, NamedTuple

from train_commute_companion.model import (
    AdviceRecord,
    DirectionState,
    EventKind,
    LegState,
    Obligation,
    OccurrenceKey,
    Severity,
    Signature,
    TrainId,
    train_ident,
)

PollFailure = Literal["auth", "rate", "provider", "parse", "timeout"]


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


@dataclass(frozen=True, slots=True)
class Journey:
    legs: tuple[LegProgress, ...]
    source: LockSource
    commitment: Commitment
    planned_final_arrival: datetime


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
    keys.pop(_LIVE_PREFIX + fact)
    keys.pop(_SENT_PREFIX + fact, None)


def unclear_slot(train_key: str) -> str:
    return f"unclear#{train_key}"


@dataclass(frozen=True, slots=True)
class BaselineVersion:
    version: int
    signature: Signature
    effective_from: date
    confirmed: bool


@dataclass(frozen=True, slots=True)
class BaselineHistory:
    versions: tuple[BaselineVersion, ...] = ()
    floor: int = 0  # highest number retired (re-seed or cancellation): numbers never repeat

    def in_force(self, day: date) -> BaselineVersion | None:
        live = [v for v in self.versions if v.effective_from <= day]
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
    def warns(self) -> bool:
        return "|warn:" in self.key

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

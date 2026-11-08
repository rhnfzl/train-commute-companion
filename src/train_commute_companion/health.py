"""Board health and the cannot_see_train conditions (7.11, 7.12 rules 13 and 20). No clock."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from train_commute_companion.calendar import to_utc
from train_commute_companion.model import Observation, Provenance, TrainId
from train_commute_companion.state import PollRecord

FRESH_FOR = timedelta(minutes=5)
ZONE = timedelta(minutes=30)
FAILED_POLLS = 2


class BoardPhase(StrEnum):
    UNSEEN = "unseen"
    STALE = "stale"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    AUTH_REJECTED = "auth_rejected"


@dataclass(frozen=True, slots=True)
class BoardHealth:
    fresh: bool
    last_ok_at: datetime | None
    consecutive_failures: int
    auth_failed: bool

    @property
    def phase(self) -> BoardPhase:
        if self.auth_failed:
            return BoardPhase.AUTH_REJECTED
        if self.last_ok_at is None:
            return BoardPhase.UNSEEN
        if not self.fresh:
            return BoardPhase.STALE
        if self.consecutive_failures:
            return BoardPhase.DEGRADED
        return BoardPhase.HEALTHY


@dataclass(frozen=True, slots=True)
class WatchedLeg:
    train: TrainId
    departure: datetime
    riding_or_watched: bool
    observation: Observation
    dep_provenance: Provenance
    arr_provenance: Provenance
    last_seen_at: datetime | None  # last fresh healthy answer that contained this leg


def board_health(records: Sequence[PollRecord], now: datetime) -> BoardHealth:
    ordered = sorted(records, key=lambda r: to_utc(r.at))
    last_ok = next((r.at for r in reversed(ordered) if r.ok), None)
    failures = 0
    for r in reversed(ordered):
        if r.ok:
            break
        failures += 1
    auth = bool(ordered) and not ordered[-1].ok and ordered[-1].error == "auth"
    fresh = last_ok is not None and timedelta() <= to_utc(now) - to_utc(last_ok) < FRESH_FOR
    return BoardHealth(fresh, last_ok, failures, auth)


def _leg_blind(leg: WatchedLeg, departure: datetime, now: datetime) -> str | None:
    if leg.observation is not Observation.SEEN:
        return f"the train is {leg.observation.value} in the answer"
    if now < departure and leg.dep_provenance is not Provenance.REPORTED:
        return "no reported departure time"
    if leg.riding_or_watched and leg.arr_provenance is not Provenance.REPORTED:
        return "no reported arrival time"
    if leg.last_seen_at is None or not timedelta() <= now - to_utc(leg.last_seen_at) < FRESH_FOR:
        return "the train is not in a fresh answer"
    return None


def blindness(legs: Sequence[WatchedLeg], health: BoardHealth, now: datetime) -> tuple[bool, str]:
    now = to_utc(now)
    board_reason = None
    if not health.fresh:
        board_reason = "the board is older than 5 minutes"
    elif health.consecutive_failures >= FAILED_POLLS:
        board_reason = "the last polls failed"
    for leg in legs:
        # departed and neither ridden nor watched means finished (rule 13: unfinished legs only)
        departure = to_utc(leg.departure)
        if not (leg.riding_or_watched or timedelta(0) <= departure - now <= ZONE):
            continue
        reason = board_reason or _leg_blind(leg, departure, now)
        if reason:
            return True, reason
    return False, ""


def offline(health: BoardHealth, any_in_zone_or_riding: bool) -> bool:
    return health.phase is not BoardPhase.HEALTHY and not any_in_zone_or_riding

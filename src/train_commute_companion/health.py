"""Board health and the cannot_see_train conditions (7.11, 7.12 rules 13 and 20). No clock."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from train_commute_companion.model import Observation, Provenance, TrainId

FRESH_FOR = timedelta(minutes=5)
ZONE = timedelta(minutes=30)
FAILED_POLLS = 2


@dataclass(frozen=True, slots=True)
class PollRecord:
    at: datetime
    ok: bool
    error: Literal["auth", "rate", "provider", "parse", "timeout"] | None


@dataclass(frozen=True, slots=True)
class BoardHealth:
    fresh: bool
    last_ok_at: datetime | None
    consecutive_failures: int
    auth_failed: bool


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
    ordered = sorted(records, key=lambda r: r.at)
    last_ok = next((r.at for r in reversed(ordered) if r.ok), None)
    failures = 0
    for r in reversed(ordered):
        if r.ok:
            break
        failures += 1
    auth = bool(ordered) and not ordered[-1].ok and ordered[-1].error == "auth"
    return BoardHealth(last_ok is not None and now - last_ok < FRESH_FOR, last_ok, failures, auth)


def cannot_see(legs: Sequence[WatchedLeg], health: BoardHealth, now: datetime) -> tuple[bool, str]:
    board_reason = None
    if not health.fresh:
        board_reason = "the board is older than 5 minutes"
    elif health.consecutive_failures >= FAILED_POLLS:
        board_reason = "the last polls failed"
    for leg in legs:
        if not (leg.riding_or_watched or leg.departure - now <= ZONE):
            continue
        reason = board_reason
        if reason is None:
            if leg.observation is not Observation.SEEN:
                reason = f"the train is {leg.observation.value} in the answer"
            elif now < leg.departure and leg.dep_provenance is not Provenance.REPORTED:
                reason = "no reported departure time"
            elif leg.riding_or_watched and leg.arr_provenance is not Provenance.REPORTED:
                reason = "no reported arrival time"
            elif leg.last_seen_at is None or now - leg.last_seen_at >= FRESH_FOR:
                reason = "the train is not in a fresh answer"
        if reason:
            return True, reason
    return False, ""


def offline(health: BoardHealth, any_in_zone_or_riding: bool) -> bool:
    unhealthy = not health.fresh or health.consecutive_failures > 0 or health.auth_failed
    return unhealthy and not any_in_zone_or_riding

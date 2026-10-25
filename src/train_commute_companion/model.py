"""Core value types. All times are timezone-aware (Europe/Amsterdam)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal


class Direction(StrEnum):
    OUTBOUND = "outbound"
    RETURN = "return"


class LegKind(StrEnum):
    OK = "ok"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    CUT_SHORT = "cut_short"


class Observation(StrEnum):
    SEEN = "seen"
    UNCERTAIN = "uncertain"
    MISSING = "missing"


class Provenance(StrEnum):
    REPORTED = "reported"
    PLANNED = "planned"
    MISSING = "missing"


class TransferState(StrEnum):
    OK = "ok"
    AT_RISK = "at_risk"
    BROKEN = "broken"


class Severity(StrEnum):
    LOUD = "loud"
    SILENT = "silent"


class EventKind(StrEnum):
    CANCELLED = "cancelled"
    TRANSFER_BROKEN = "transfer_broken"
    TRANSFER_AT_RISK = "transfer_at_risk"
    MISSED_FIRST_TRAIN = "missed_first_train"
    MISSED_TRANSFER = "missed_transfer"
    LEAVE_NOW = "leave_now"
    PLATFORM_CHANGE = "platform_change"
    CUT_SHORT = "cut_short"
    ROUTE_CHANGED = "route_changed"
    CANNOT_SEE_TRAIN = "cannot_see_train"
    CAN_SEE_TRAIN_AGAIN = "can_see_train_again"
    KEY_REJECTED = "key_rejected"
    NO_CONNECTION = "no_connection"
    MISSING = "missing"
    STOPPED_FOLLOWING = "stopped_following"
    PROFILE_REFUSED = "profile_refused"
    STORAGE_FAILED = "storage_failed"
    DELAYED = "delayed"
    ALL_GOOD = "all_good"
    NEARLY_ON_TIME = "nearly_on_time"
    BACK_TO_NORMAL = "back_to_normal"
    RESTORED = "restored"
    WORKS_AHEAD = "works_ahead"
    TIMETABLE_ADOPTED = "timetable_adopted"
    EARLIER_TRAIN_WITHDRAWN = "earlier_train_withdrawn"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class TimeObs:
    planned: datetime
    value: datetime | None
    provenance: Provenance
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TrainId:
    operating_date: date
    train_number: str
    planned_departure: datetime


@dataclass(frozen=True, slots=True)
class Stop:
    code: str
    name: str
    planned_arrival: datetime | None
    planned_departure: datetime | None
    arrival: TimeObs | None
    departure: TimeObs | None
    cancelled: bool
    passing: bool
    planned_track: str | None
    actual_track: str | None


@dataclass(frozen=True, slots=True)
class Leg:
    train: TrainId
    category: str
    stops: tuple[Stop, ...]
    cancelled: bool
    part_cancelled: bool
    change_possible: bool | None
    reachable: bool | None
    alternative_transport: bool
    messages: tuple[str, ...]
    transfer_time_to_next: int | None
    transfer_message_min: int | None
    cross_platform: bool | None

    @property
    def board(self) -> Stop:
        return self.stops[0]

    @property
    def alight(self) -> Stop:
        return self.stops[-1]


@dataclass(frozen=True, slots=True)
class Connection:
    legs: tuple[Leg, ...]
    status: str
    ctx_recon: str | None

    @property
    def origin(self) -> str:
        return self.legs[0].board.code

    @property
    def destination(self) -> str:
        return self.legs[-1].alight.code

    @property
    def transfer_stations(self) -> tuple[str, ...]:
        return tuple(leg.alight.code for leg in self.legs[:-1])

    @property
    def is_train_only(self) -> bool:
        return not any(leg.alternative_transport for leg in self.legs)


@dataclass(frozen=True, slots=True)
class AlertEvent:
    profile_id: str
    profile_name: str
    direction: Direction | None
    service_date: date | None
    leg_index: int | None
    kind: EventKind
    severity: Severity
    train: TrainId | None
    text: str
    key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OccurrenceKey:
    profile_id: str
    direction: Direction
    service_date: date


@dataclass(frozen=True, slots=True)
class DatedSchedule:
    key: OccurrenceKey
    first_departure: datetime
    window_start: datetime
    window_end: datetime
    auto_lock_at: datetime


@dataclass(frozen=True, slots=True)
class UsualJourney:
    first_train: time
    transfers: tuple[str, ...]
    window: tuple[time, time]
    buffer_min: int
    earlier_train_advice: bool
    day_offset: int


@dataclass(frozen=True, slots=True)
class AlertSettings:
    earlier_train_threshold_min: int = 10
    earlier_train_min_saving_min: int = 5
    latest_acceptable_arrival: time | None = None
    quiet_hours: tuple[time, time] | None = None


@dataclass(frozen=True, slots=True)
class LocationSettings:
    source: Literal["ha", "telegram", "owntracks"]
    entity: str | None
    zones: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    traveller: str
    days: frozenset[int]
    extra_dates: frozenset[date]
    skip_public_holidays: bool
    home_station: str
    work_station: str
    outbound: UsualJourney
    return_: UsualJourney
    transfer_min: Mapping[str, int]
    alerts: AlertSettings
    location: LocationSettings | None

    def journey(self, d: Direction) -> UsualJourney:
        return self.outbound if d is Direction.OUTBOUND else self.return_

    def origin(self, d: Direction) -> str:
        return self.home_station if d is Direction.OUTBOUND else self.work_station

    def destination(self, d: Direction) -> str:
        return self.work_station if d is Direction.OUTBOUND else self.home_station


@dataclass(frozen=True, slots=True)
class Config:
    revision: int
    profiles: tuple[Profile, ...]

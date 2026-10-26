"""Core value types. All times are timezone-aware (Europe/Amsterdam)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
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
    CHOOSE_REFUSED = "choose_refused"
    NEXT_CONNECTION = "next_connection"


@dataclass(frozen=True, slots=True)
class TimeObs:
    planned: datetime | None  # None when the provider reported a time without a planned one
    value: datetime | None
    provenance: Provenance
    observed_at: datetime


@dataclass(frozen=True, slots=True, eq=False)
class TrainId:
    operating_date: date
    train_number: str
    planned_departure: datetime

    # same-zone datetime equality ignores fold, so compare instants (first and second 02:30 differ)
    def _key(self) -> tuple[date, str, datetime]:
        return (self.operating_date, self.train_number, self.planned_departure.astimezone(UTC))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TrainId) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())


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
    uic: str | None = None  # provider station number; planned works list stations by it


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
class ClassifiedLeg:
    leg: Leg
    kind: LegKind
    observation: Observation
    dep_delay_min: int | None  # None when no reported time; 0 for a cancelled train (planned time)
    arr_delay_min: int | None  # None unless the train reaches the alight station
    last_served: Stop | None  # set only for CUT_SHORT


@dataclass(frozen=True, slots=True)
class TransferCheck:
    state: TransferState
    margin_min: int | None  # None when a leg has no usable time
    minimum: int
    reason: str


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
class SignatureLeg:
    board: str
    alight: str
    departure: time  # local clock time
    day_offset: int  # days after the service date
    duration_min: int


@dataclass(frozen=True, slots=True)
class Signature:
    legs: tuple[SignatureLeg, ...]

    @property
    def transfers(self) -> tuple[str, ...]:
        return tuple(leg.alight for leg in self.legs[:-1])


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
class AdviceRecord:
    advised_train: TrainId | None
    sent_at: datetime | None
    withdrawn: bool
    # the usual journey's planned arrival, read from a healthy answer before any relock when no
    # baseline exists yet: the 7.6 reference, never a replacement's arrival
    usual_arrival: datetime | None = None


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
    station_radius_m: int = 150  # last and defaulted, so stored configs still decode


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

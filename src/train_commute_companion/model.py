"""Core value types. All times are timezone-aware (Europe/Amsterdam)."""

import difflib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal, Self

FOLLOW_LIMIT = timedelta(minutes=150)


class Direction(StrEnum):
    OUTBOUND = "outbound"
    RETURN = "return"


def present[T](value: T | None) -> T:
    """A value an invariant says is there. Unlike `assert`, the check survives `python -O`."""
    if value is None:
        raise AssertionError("an invariant promised a value here")
    return value


def direction_key(profile_id: str, d: Direction) -> str:
    """The stored key of one profile's direction."""
    return f"{profile_id}|{d.value}"


def split_direction_key(key: str) -> tuple[str, str]:
    """The profile id and direction of a `direction_key`."""
    pid, _, d = key.rpartition("|")  # a profile id may contain "|"; the direction never does
    return pid, d


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
    RECOVERY_UPDATE = "recovery_update"
    RECOVERY_MONITORING = "recovery_monitoring"
    DISRUPTION = "disruption"
    DISRUPTION_CLEARED = "disruption_cleared"
    CROWD_CHANGE = "crowd_change"
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
    WORKS_DIGEST = "works_digest"
    COMPENSATION_REMINDER = "compensation_reminder"
    TIMETABLE_ADOPTED = "timetable_adopted"
    EARLIER_TRAIN_WITHDRAWN = "earlier_train_withdrawn"
    OFFLINE = "offline"
    CHOOSE_REFUSED = "choose_refused"
    NEXT_CONNECTION = "next_connection"
    AT_STATION = "at_station"  # Q1: the train followed now, once at the origin station
    STATION_ASK = "station_ask"  # a question with answer buttons: are you boarding here?
    STATION_REMEMBER = "station_remember"  # a question: remember this boarding station?
    DATA_UNCLEAR = "data_unclear"


DELAY_NEWS = frozenset({EventKind.DELAYED, EventKind.NEARLY_ON_TIME, EventKind.BACK_TO_NORMAL})


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


def train_ident(t: TrainId) -> str:
    return f"{t.train_number}@{t.planned_departure.isoformat()}"


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
    # the arrival side's platforms; `planned_track`/`actual_track` are the departure side's
    planned_arrival_track: str | None = None
    actual_arrival_track: str | None = None
    accessibility_notes: tuple[str, ...] = ()
    exit_side: Literal["left", "right"] | None = None
    physical_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None


CrowdForecast = Literal["low", "medium", "high"]
TransportMode = Literal["TRAIN", "BUS", "METRO", "TRAM", "FERRY", "WALK"]


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
    crowd_forecast: CrowdForecast | None = None
    transport_mode: TransportMode | None = None
    operator: str | None = None
    service_reference: str | None = None

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
class LiveDelay:
    """A delay an alert would state, before `lifecycle.keep_stated` judges it as news (7.5)."""

    tag: str  # the delay's own memory slot: "d" departure, "a" arrival, "" the only one
    minutes: int | None  # None: NS reported none, which keeps the last stated one


@dataclass(frozen=True, slots=True)
class ToldDelay:
    """A delay an alert states, with the memory entry that says it is still the latest."""

    slot: str
    said: str
    previous_min: int | None = None  # recheck a held update after a repeat-step edit


@dataclass(frozen=True, slots=True)
class AlertFacts:
    """What an alert states, as data, so nothing reads its key or text back. The key stays a
    plain string for the news ledger, in the form state files already hold."""

    delays: tuple[LiveDelay, ...] = ()
    told: tuple[ToldDelay, ...] = ()
    moved_slot: str = ""  # a platform message's memory slot, the same for any platform it names
    back_on_plan: bool = False  # that platform is the planned one again
    subject: str = ""  # "your <leg>", which a delay's recovery message opens with
    on: str = ""  # a retained warning's "if you are on the ..." prefix; "" for any other alert
    named: tuple[str, ...] = ()  # the replacement a warning names, by `train_ident`
    next_clause: str = ""  # the text that names it, right after the warning itself
    unclear: str = ""  # the fingerprint of the values a data-unclear message states
    twin_note: str = ""  # the 7.13 service date note closing the text, while there are twins
    recovery_selection: str = ""
    recovery_revision: int = 0
    recovery_issue: str = ""
    crowd_slot: str = ""
    crowd_forecast: CrowdForecast | None = None
    crowd_note: str = ""
    disruption_id: str = ""
    disruption_revision: int = 0
    disruption_instance: str = ""
    disruption_targets: frozenset[str] | None = None
    arrival_record: str = ""
    arrival_revision: int = 0
    # a question to the user: its id and its (answer, label) buttons; empty for any other alert
    prompt_id: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    # silent by a rule, not by its kind (a guess at an other station): never escalated
    held: bool = False
    # None marks stored alerts written before arrival-limit protection was recorded.
    past_latest_arrival: bool | None = None


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
    # None: stored before facts were kept, or not a journey alert; read with `alert_facts`
    facts: AlertFacts | None = None

    @property
    def body(self) -> str:
        """The text after the profile name that opens it."""
        return self.text.removeprefix(f"{display_name(self.profile_name)}: ")


def legacy_facts(e: AlertEvent) -> AlertFacts:
    """What an alert held in a state file from before facts were kept states, read from its key
    and text: the only place that reads them back, so those alerts keep rules 5(a) and 5(b)."""
    told: tuple[ToldDelay, ...] = ()
    base, sep, vals = e.key.rpartition("|stated+")
    if sep and e.kind in DELAY_NEWS:
        base = EventKind.DELAYED.value + base[len(e.kind.value) :]
        said = vals.split(",")
        slots = [base + t for t in ("#d", "#a")] if len(said) == 2 else [base]
        told = tuple(ToldDelay(x, v) for x, v in zip(slots, said, strict=True))
    part = next((x for x in e.key.split("|")[1:] if "@" in x and not x.startswith("stated")), "")
    i = e.text.find(" (service date ")
    head, note = (e.text, "") if i < 0 else (e.text[:i], e.text[i:])
    j = head.find(" Next good connection:")
    return AlertFacts(
        told=told,
        on="retained" if ":retained:" in e.key else "",
        named=tuple(x for x in part.split(":retained:")[0].split(",") if x),
        next_clause="" if j < 0 else head[j:],
        unclear=e.key.rpartition(":")[2] if e.kind is EventKind.DATA_UNCLEAR else "",
        twin_note=note,
    )


def alert_facts(e: AlertEvent) -> AlertFacts:
    """What an alert states. The one reading of a stored `facts` of None."""
    return legacy_facts(e) if e.facts is None else e.facts


def held(e: AlertEvent) -> AlertEvent:
    """Silent, and kept silent: escalation (rule 4) never makes it loud on a repeat."""
    return replace(e, severity=Severity.SILENT, facts=replace(alert_facts(e), held=True))


class AdvicePhase(StrEnum):
    EMPTY = "empty"
    PENDING = "pending"
    DELIVERED = "delivered"
    WITHDRAWN_UNSENT = "withdrawn_unsent"
    WITHDRAWN_DELIVERED = "withdrawn_delivered"


@dataclass(frozen=True, slots=True)
class AdviceRecord:
    advised_train: TrainId | None
    sent_at: datetime | None
    withdrawn: bool
    # the usual journey's planned arrival, read from a healthy answer before any relock when no
    # baseline exists yet: the 7.6 reference, never a replacement's arrival
    usual_arrival: datetime | None = None
    # the episode's LEAVE_NOW reached the user; pending until then. An undelivered episode that
    # stops holding is ended silently (`withdrawn` with this False), so it can start again.
    delivered: bool = field(kw_only=True)

    @property
    def phase(self) -> AdvicePhase:
        if self.withdrawn:
            return (
                AdvicePhase.WITHDRAWN_DELIVERED if self.delivered else AdvicePhase.WITHDRAWN_UNSENT
            )
        if self.advised_train is None:
            return AdvicePhase.EMPTY
        return AdvicePhase.DELIVERED if self.delivered else AdvicePhase.PENDING

    @property
    def active(self) -> bool:
        return self.phase in (AdvicePhase.PENDING, AdvicePhase.DELIVERED)

    def withdraw(self) -> "AdviceRecord":
        return replace(self, withdrawn=True)

    def accept_delivery(self) -> "AdviceRecord":
        if self.advised_train is None:
            raise ValueError("advice episode is missing")
        return replace(self, delivered=True)


@dataclass(frozen=True, slots=True)
class OccurrenceKey:
    profile_id: str
    direction: Direction
    service_date: date


def okey(k: OccurrenceKey) -> str:
    return f"{k.profile_id}|{k.direction.value}|{k.service_date.isoformat()}"


Mode = Literal["fixed", "window"]
Pick = Literal["earliest", "earliest_silent", "on_leave"]


@dataclass(frozen=True, slots=True)
class DatedSchedule:
    key: OccurrenceKey
    first_departure: datetime
    window_start: datetime
    window_end: datetime
    # never persisted: rebuilt from the profile each time (only OccurrenceKey is stored)
    mode: Mode = "fixed"
    pick: Pick = "earliest"
    last_departure: datetime | None = None  # window mode: the latest departure TCC may pick
    # window mode: the minimum stay ends after the latest time, so any later train is followed
    stay_capped: bool = False


@dataclass(frozen=True, slots=True)
class DayOverride:
    """One weekday's settings that differ from the direction's own; None keeps the default."""

    mode: Mode | None = None
    window: tuple[time, time] | None = None
    pick: Pick | None = None
    min_stay_min: int | None = None


@dataclass(frozen=True, slots=True)
class UsualJourney:
    first_train: time
    transfers: tuple[str, ...]
    window: tuple[time, time]
    buffer_min: int
    earlier_train_advice: bool
    day_offset: int
    first_train_by_weekday: Mapping[str, time] = field(default_factory=dict)
    active_lead_min: int | None = None  # Q9: end of the quiet early watch; None is buffer + 15
    # every field below is defaulted, so stored config snapshots still decode
    mode: Mode = "fixed"  # window: any train between window[0] and window[1], no usual train
    pick: Pick = "earliest"
    min_stay_min: int | None = None  # window days: start no earlier than arrival + this
    by_weekday: Mapping[str, DayOverride] = field(default_factory=dict)
    # False: window mode with no first_train; window[0] stands in where code needs a time
    first_train_set: bool = True
    # other boarding stations TCC follows by itself when the phone is seen there
    other_stations: tuple[str, ...] = ()


NotificationChannel = Literal["primary", "email", "webhook"]


NotificationCategory = Literal[
    "journey",
    "transfers",
    "advice",
    "delays",
    "platforms",
    "crowding",
    "planned_works",
    "disruptions",
    "routine",
]


@dataclass(frozen=True, slots=True)
class AlertSettings:
    earlier_train_threshold_min: int = 10
    earlier_train_min_saving_min: int = 5
    latest_acceptable_arrival: time | None = None
    quiet_hours: tuple[time, time] | None = None
    disabled_categories: frozenset[NotificationCategory] = frozenset()
    channels: Mapping[NotificationCategory, frozenset[NotificationChannel]] = field(
        default_factory=dict
    )
    compensation_reminder: bool = False
    # a second or later miss from home (work on the way back) of the same day and direction
    # is silent: TCC chose that train itself after the first miss
    quiet_repeat_misses: bool = False
    delay_min: int = 3
    delay_step_min: int = 3
    weather: bool = True
    leave_by: bool = True
    weather_allowance: bool = True


LocationSource = Literal["ha", "telegram", "owntracks"]


@dataclass(frozen=True, slots=True)
class LocationSettings:
    source: LocationSource
    entity: str | None
    zones: Mapping[str, str]
    station_radius_m: int = 150  # last and defaulted, so stored configs still decode
    # Q12, source ha only: place role -> a Wi-Fi tracker (home/not_home), a motion activity sensor
    wifi: Mapping[str, str] = field(default_factory=dict)
    activity_entity: str | None = None
    # other boarding station: how near a fix must be, how long it must stay, and whether a
    # confirmed new station is offered to be remembered
    other_station_radius_m: int = 500
    other_station_dwell_min: int = 0
    ask_to_remember_station: bool = True


@dataclass(frozen=True, slots=True)
class AccessibilitySettings:
    require_accessible: bool = False


@dataclass(frozen=True, slots=True)
class WorksDigestSettings:
    day: int
    time: time


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
    pauses: tuple[tuple[date, date], ...] = ()
    accessibility: AccessibilitySettings = field(default_factory=AccessibilitySettings)
    works_digest: WorksDigestSettings | None = None

    def paused_on(self, service_date: date) -> bool:
        return any(start <= service_date <= end for start, end in self.pauses)

    def journey(self, d: Direction) -> UsualJourney:
        return self.outbound if d is Direction.OUTBOUND else self.return_

    def journey_on(self, d: Direction, commute_day: date) -> UsualJourney:
        """Resolve the commute weekday before applying a return's service-day offset: its first
        train, then its `by_weekday` override of mode, window, pick and minimum stay."""
        return resolve_day(
            self.journey(d), _WEEKDAY_KEYS[commute_day.weekday()], self.location is not None
        )

    def origin(self, d: Direction) -> str:
        return self.home_station if d is Direction.OUTBOUND else self.work_station

    def destination(self, d: Direction) -> str:
        return self.work_station if d is Direction.OUTBOUND else self.home_station


_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _shift(value: time, minutes: int) -> time:
    minute = (value.hour * 60 + value.minute + minutes) % 1440
    return time(minute // 60, minute % 60)


def resolve_day(journey: UsualJourney, weekday: str, has_location: bool) -> UsualJourney:
    """The journey as it runs on `weekday` (mon..sun). The one place a day override and the
    pick fallback are applied, so the profile check and the runtime never disagree."""
    o = journey.by_weekday.get(weekday, DayOverride())
    mode = o.mode or journey.mode
    first = journey.first_train_by_weekday.get(weekday, journey.first_train)
    if o.window is not None:
        window = o.window
    elif mode == "window":
        window = journey.window
    elif journey.mode == "window":  # a fixed day of a window direction: the default watch
        window = (_shift(first, -60), _shift(first, 120))
    else:
        delta = (first.hour * 60 + first.minute) - (
            journey.first_train.hour * 60 + journey.first_train.minute
        )
        window = (_shift(journey.window[0], delta), _shift(journey.window[1], delta))
    pick = o.pick or journey.pick
    if pick == "on_leave" and not has_location:
        pick = "earliest_silent"  # nothing can tell when you leave
    stay = o.min_stay_min if o.min_stay_min is not None else journey.min_stay_min
    return replace(
        journey,
        first_train=first,
        window=window,
        mode=mode,
        pick=pick,
        min_stay_min=stay,
        first_train_set=journey.first_train_set or weekday in journey.first_train_by_weekday,
    )


def display_name(name: str) -> str:
    return name.replace("_", " ").title()


def station_key(text: str, *, accents: bool = True) -> str:
    """A station name for lookup: case and punctuation ignored, accents optionally too."""
    folded = unicodedata.normalize("NFC" if accents else "NFKD", text.casefold())
    if not accents:
        folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", " ", folded).strip()


class Stations(frozenset[str]):
    """Known station codes that also carry each station's names, so a profile may name a
    station instead of giving its code. Any provider can fill it; a plain frozenset of codes
    still works everywhere and simply knows no names."""

    labels: Mapping[str, tuple[str, ...]]  # code -> its names, the display name first
    _exact: dict[str, tuple[str, ...]]
    _loose: dict[str, tuple[str, ...]]

    def __new__(cls, labels: Mapping[str, Sequence[str]]) -> Self:
        self = super().__new__(cls, labels)
        self.labels = {code: tuple(names) for code, names in labels.items()}
        self._exact = self._index(accents=True)
        self._loose = self._index(accents=False)
        return self

    def __reduce__(self) -> tuple[type["Stations"], tuple[dict[str, tuple[str, ...]]]]:
        return Stations, (dict(self.labels),)  # a copy keeps the names

    def _index(self, *, accents: bool) -> dict[str, tuple[str, ...]]:
        out: dict[str, tuple[str, ...]] = {}
        for code, names in self.labels.items():
            for key in dict.fromkeys(station_key(n, accents=accents) for n in names):
                if key:
                    out[key] = (*out.get(key, ()), code)
        return out

    def name(self, code: str) -> str:
        return next(iter(self.labels.get(code, ())), code)

    def lookup(self, text: str) -> tuple[str, ...]:
        """Codes for a name: a code itself first, then the name as written with accents, then
        without them. More than one code means the name is ambiguous; it is never guessed."""
        if text in self:
            return (text,)
        return self._exact.get(station_key(text)) or self._loose.get(
            station_key(text, accents=False), ()
        )

    def closest(self, text: str) -> str | None:
        """A station name close to `text`, only ever offered as a suggestion."""
        close = difflib.get_close_matches(station_key(text, accents=False), self._loose, 1)
        return self.name(self._loose[close[0]][0]) if close else None


@dataclass(frozen=True, slots=True)
class ProfileProblem:
    profile_id: str | None
    path: str
    message: str
    suggestion: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    revision: int
    profiles: tuple[Profile, ...]


class LegState(StrEnum):
    PENDING = "pending"
    BOARDING = "boarding"
    RIDING = "riding"
    DONE = "done"
    MISSED = "missed"
    CANCELLED = "cancelled"
    RELOCKED = "relocked"


class DirectionState(StrEnum):
    IDLE = "idle"
    LOCKED = "locked"
    RIDING = "riding"
    DONE = "done"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class Ctx:
    profile: Profile
    direction: Direction
    schedule: DatedSchedule
    signature: Signature | None
    now: datetime
    # the stations the user asked TCC to remember for this profile and direction
    remembered: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Leaving:
    service_date: date | None
    at: datetime


@dataclass(frozen=True, slots=True)
class NotTravelling:
    service_date: date | None
    whole_day: bool


@dataclass(frozen=True, slots=True)
class Obligation:
    id: str
    event: AlertEvent
    occurrence: OccurrenceKey | None
    look_ahead: tuple[str, Direction, date] | None
    route_revision: int
    baseline_version: int | None
    fact_key: str
    expires_at: datetime | None
    attempts: int
    webhook_copied: bool = False  # its webhook copy was made (stored before: made next pass)
    target_ids: frozenset[str] | None = None
    primary_target_pending: bool = False
    digest_token: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookCopy:
    """A durable webhook copy of an alert, apart from its phone obligation: a receiver can
    never change phone delivery. `id` is `webhook.event_id(obligation.id)`; `done` holds the
    opaque authentication-scoped ID of each destination that accepted it, never a URL or secret.
    `last_try` is the latest pass with an actual try. `target_ids` pins original recipients;
    None is legacy state whose unknown auxiliary audience cannot be rebound."""

    id: str
    obligation: Obligation
    done: tuple[str, ...] = ()
    first_try: datetime | None = None
    last_try: datetime | None = None
    target_ids: frozenset[str] | None = None

"""Location zones and events (7.2, 8). Location only adds: it never silences an alert. Pure."""

import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, get_args

from train_commute_companion.calendar import to_utc
from train_commute_companion.model import LocationSettings, Profile

_EARTH_M = 6_371_000.0
STALE_LOCATION = timedelta(minutes=10)
REPORT_EVERY = timedelta(minutes=2)  # each event costs a live request per occurrence
PLACE_ROLES = ("home", "work")


@dataclass(frozen=True, slots=True)
class Zone:
    name: str
    lat: float
    lon: float
    radius_m: float


LocationKind = Literal["enter", "leave", "report", "still"]
LOCATION_KINDS: tuple[LocationKind, ...] = get_args(LocationKind)


@dataclass(frozen=True, slots=True)
class Evidence:
    """Q12: what a source knows beside the position, each part optional. Only the core judges
    it (a riding sign, or proof that a train was missed)."""

    speed: float | None = None  # m/s of the fix behind the event
    activity: str | None = None  # the phone's motion activity, as the phone names it
    activity_at: datetime | None = None  # when the phone reported that activity
    wifi: bool | None = None  # the event zone's place Wi-Fi: True connected, False not
    # the fix lies inside station_radius_m of the event's station, accuracy known and within it
    # too: proof the user waits there, also on a first fix that an other station's wider circle
    # placed (`view.in_station_circle`)
    in_station: bool | None = None


NO_EVIDENCE = Evidence()


@dataclass(frozen=True, slots=True)
class LocationEvent:
    """`still` (Q3): a shell vouches that its source still places the user in `zone` at `at`
    with no newer fix and no exit; never produced by `next_zone`, stored as presence."""

    zone: str | None  # a station zone is named by its station code
    kind: LocationKind
    at: datetime  # when the position was taken
    evidence: Evidence = NO_EVIDENCE


@dataclass(frozen=True, slots=True)
class Fix:
    lat: float
    lon: float
    at: datetime
    acc: float | None = None
    regions: frozenset[str] = frozenset()
    speed: float | None = None  # m/s, when the source reports one (Q12 riding sign)


def number(v: object) -> float | None:
    """A finite JSON number (not a bool), else None."""
    if not isinstance(v, int | float) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except OverflowError:  # an integer too large for a float
        return None
    return f if math.isfinite(f) else None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_M * math.asin(math.sqrt(h))


def clear_of(fix: Fix, zones: Sequence[Zone]) -> bool:
    """Q12: the whole uncertainty circle of `fix` lies outside every zone."""
    acc = fix.acc or 0.0
    return all(distance_m(fix.lat, fix.lon, z.lat, z.lon) > z.radius_m + acc for z in zones)


def resolve_zone(lat: float, lon: float, zones: Sequence[Zone]) -> str | None:
    """The smallest zone containing the point (a station inside a city zone wins), else None."""
    inside = [z for z in zones if distance_m(lat, lon, z.lat, z.lon) <= z.radius_m]
    return min(inside, key=lambda z: z.radius_m).name if inside else None


def fix_zone(fix: Fix, loc: LocationSettings, zones: Sequence[Zone]) -> str | None:
    """The zone of a fix's position, else a place the source itself names (OwnTracks regions),
    by the configured name the core compares."""
    zone = resolve_zone(fix.lat, fix.lon, zones)
    if zone is not None:
        return zone
    names = (loc.zones.get(role, role) for role in PLACE_ROLES)
    return next((n for n in names if n in fix.regions), None)


def station_zones(
    profile: Profile,
    radius_m: float,
    positions: Mapping[str, tuple[float, float]],
    running: Iterable[str] = (),
) -> list[Zone]:
    """A zone per station of the route and of `running` that has a known position."""
    codes = {
        profile.home_station,
        profile.work_station,
        *profile.outbound.transfers,
        *profile.return_.transfers,
        *running,
    }
    return [Zone(c, *positions[c], float(radius_m)) for c in sorted(codes) if c in positions]


class ZoneReportPhase(StrEnum):
    OUTSIDE = "outside"
    READY = "ready"
    THROTTLED = "throttled"


@dataclass(frozen=True, slots=True)
class ZoneTrack:
    """The zone a source last placed the user in, and when each zone last started the timer."""

    zone: str | None = None
    reported: Mapping[str, datetime] = field(default_factory=dict)

    def phase_at(self, at: datetime) -> ZoneReportPhase:
        if self.zone is None:
            return ZoneReportPhase.OUTSIDE
        last = self.reported.get(self.zone)
        if last is None or to_utc(at) - to_utc(last) >= REPORT_EVERY:
            return ZoneReportPhase.READY
        return ZoneReportPhase.THROTTLED


def seed_zone(zone: str | None, at: datetime) -> ZoneTrack:
    """A source's first fix sends nothing (7.2); inside a zone it starts the report timer."""
    return ZoneTrack(zone, {zone: at} if zone else {})


def next_zone(
    track: ZoneTrack, zone: str | None, at: datetime
) -> tuple[ZoneTrack, list[tuple[str, LocationKind]]]:
    """The track after a fix in `zone`, and its events: leave then enter on a change, else a
    report once per `REPORT_EVERY`. An enter already says the user is there, so it starts the
    timer: a report seconds later would spend a live request to tell the core nothing new. The
    cost: a throttled fix is dropped, not deferred, so one just after a departure raises no
    missed leg (rule 7) until a later fix in the zone, however long that takes. A dispatch that
    fails or goes unanswered takes the timer back through `undelivered`."""
    events: list[tuple[str, LocationKind]] = []
    if zone != track.zone:
        if track.zone is not None:
            events.append((track.zone, "leave"))
        if zone is not None:
            events.append((zone, "enter"))
    elif zone is not None and track.phase_at(at) is ZoneReportPhase.READY:
        events.append((zone, "report"))
    # the last event, if any, is this zone's enter or report; a plain leave starts no timer
    started = {zone: at} if zone is not None and events else {}
    return ZoneTrack(zone, {**track.reported, **started}), events


def without_current_report(track: ZoneTrack) -> ZoneTrack:
    """`track` when its fix never reached the core, or reached it while NS gave no answer: the
    next fix in its zone reports at once."""
    return ZoneTrack(track.zone, {z: t for z, t in track.reported.items() if z != track.zone})


async def send_zone_events(
    events: Iterable[tuple[str, LocationKind]],
    at: datetime,
    send: Callable[[LocationEvent], Awaitable[bool]],
    undelivered: Callable[[], None],
    evidence: Callable[[str], Evidence] = lambda _zone: NO_EVIDENCE,
) -> None:
    """A fix's events in order, each with the source's `evidence` for its zone (Q12). A send that
    fails, goes unanswered or raises calls `undelivered`, where the shell takes the timer back
    with `without_current_report`; a raise ends the rest."""
    try:
        for name, kind in events:
            if not await send(LocationEvent(name, kind, at, evidence(name))):
                undelivered()
    except Exception:
        undelivered()
        raise

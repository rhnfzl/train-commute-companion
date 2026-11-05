"""Location zones and events (7.2, 8). Location only adds: it never silences an alert. Pure."""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, get_args

from train_commute_companion.model import Profile

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


LocationKind = Literal["enter", "leave", "report"]
LOCATION_KINDS: tuple[LocationKind, ...] = get_args(LocationKind)


@dataclass(frozen=True, slots=True)
class LocationEvent:
    zone: str | None  # a station zone is named by its station code
    kind: LocationKind
    at: datetime  # when the position was taken


def number(v: object) -> float | None:
    """A finite JSON number (not a bool), else None."""
    if not isinstance(v, int | float) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except OverflowError:  # an integer too large for a float
        return None
    return f if math.isfinite(f) else None


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_M * math.asin(math.sqrt(h))


def resolve_zone(lat: float, lon: float, zones: Sequence[Zone]) -> str | None:
    """The smallest zone containing the point (a station inside a city zone wins), else None."""
    inside = [z for z in zones if _distance_m(lat, lon, z.lat, z.lon) <= z.radius_m]
    return min(inside, key=lambda z: z.radius_m).name if inside else None


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


@dataclass(frozen=True, slots=True)
class ZoneTrack:
    """The zone a source last placed the user in, and when each zone last started the timer."""

    zone: str | None = None
    reported: Mapping[str, datetime] = field(default_factory=dict)


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
    elif zone is not None:
        last = track.reported.get(zone)
        if last is None or at - last >= REPORT_EVERY:
            events.append((zone, "report"))
    # the last event, if any, is this zone's enter or report; a plain leave starts no timer
    started = {zone: at} if zone is not None and events else {}
    return ZoneTrack(zone, {**track.reported, **started}), events


def undelivered(track: ZoneTrack) -> ZoneTrack:
    """`track` when its fix never reached the core, or reached it while NS gave no answer: the
    next fix in its zone reports at once."""
    return ZoneTrack(track.zone, {z: t for z, t in track.reported.items() if z != track.zone})

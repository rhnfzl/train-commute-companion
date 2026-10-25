"""Location zones and events (7.2, 8). Location only adds: it never silences an alert. Pure."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

_EARTH_M = 6_371_000.0


@dataclass(frozen=True, slots=True)
class Zone:
    name: str
    lat: float
    lon: float
    radius_m: float


@dataclass(frozen=True, slots=True)
class LocationEvent:
    zone: str | None  # a station zone is named by its station code
    kind: Literal["enter", "leave", "report"]
    at: datetime  # when the position was taken
    received_at: datetime


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_M * math.asin(math.sqrt(h))


def resolve_zone(lat: float, lon: float, zones: Sequence[Zone]) -> str | None:
    """The smallest zone containing the point (a station inside a city zone wins), else None."""
    inside = [z for z in zones if _distance_m(lat, lon, z.lat, z.lon) <= z.radius_m]
    return min(inside, key=lambda z: z.radius_m).name if inside else None

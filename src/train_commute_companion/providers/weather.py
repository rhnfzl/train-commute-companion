"""Station weather helps commuters prepare for the walk to and from their train.

Forecast tips are derived estimates. Only KNMI's operational warning snapshot supplies alerts.
"""

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

import httpx

from train_commute_companion.calendar import hhmm

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WARNINGS_URL = (
    "https://api.dataplatform.knmi.nl/open-data/v1/datasets/"
    "KNMI-public-weather-warnings/versions/1.0/files"
)
FORECAST_TTL = timedelta(hours=1)
WARNING_TTL = timedelta(minutes=10)
RETRY_AFTER = timedelta(minutes=5)
MAX_STATIONS = 32
MAX_BYTES = 2_000_000
Color = Literal["yellow", "orange", "red"]
Point = tuple[float, float]  # longitude, latitude, as specified by GeoJSON
Ring = tuple[Point, ...]
Polygon = tuple[Ring, ...]


@dataclass(frozen=True, slots=True)
class WeatherHour:
    at: datetime
    temperature_c: float
    precipitation_mm: float
    snowfall_cm: float
    weather_code: int

    @property
    def tips(self) -> tuple[str, ...]:
        tips = []
        if self.precipitation_mm >= 0.2:
            tips.append("Bring a raincoat.")
        if self.snowfall_cm > 0 or self.weather_code in (71, 73, 75, 77, 85, 86):
            tips.append("Snow expected; wear shoes with grip.")
        if self.temperature_c >= 28:
            tips.append("Heat expected; carry water and seek shade.")
        if self.temperature_c <= 0:
            tips.append("Frost possible; watch your footing.")
        if self.weather_code in (56, 57, 66, 67) or (
            self.temperature_c <= 1 and self.precipitation_mm >= 0.2
        ):
            tips.append("Ice possible; paths and platforms may be slippery.")
        return tuple(tips)


@dataclass(frozen=True, slots=True)
class Forecast:
    fetched_at: datetime
    hours: tuple[WeatherHour, ...]


@dataclass(frozen=True, slots=True)
class WarningView:
    color: Color
    headline: str
    onset: datetime
    expires: datetime


@dataclass(frozen=True, slots=True)
class WeatherWarning(WarningView):
    polygons: tuple[Polygon, ...]


@dataclass(frozen=True, slots=True)
class WarningSnapshot:
    generated_at: datetime
    checked_at: datetime
    warnings: tuple[WeatherWarning, ...]

    def at(self, position: tuple[float, float], when: datetime) -> tuple[WeatherWarning, ...]:
        point = (position[1], position[0])
        return tuple(
            warning
            for warning in self.warnings
            if warning.onset <= when < warning.expires
            and any(_in_polygon(point, polygon) for polygon in warning.polygons)
        )


@dataclass(frozen=True, slots=True)
class WeatherView:
    station: str
    when: datetime
    forecast: WeatherHour | None
    fetched_at: datetime | None
    # Geometry stays inside the provider, never in runtime views, state or alert history.
    warnings: tuple[WarningView, ...]
    warnings_checked_at: datetime | None
    warnings_generated_at: datetime | None


def _object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("weather object missing")
    return raw


def _array(raw: object, maximum: int = 2000) -> list[Any]:
    if not isinstance(raw, list) or len(raw) > maximum:
        raise ValueError("weather array invalid")
    return raw


def _number(raw: object, low: float, high: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise TypeError("weather number missing")
    value = float(raw)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("weather number outside range")
    return value


def _time(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise TypeError("weather time missing")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("weather time has no timezone")
    return parsed.astimezone(UTC)


def parse_forecast(raw: object, fetched_at: datetime) -> Forecast:
    data = _object(raw)
    if data.get("utc_offset_seconds") != 0:
        raise ValueError("weather timezone must be UTC")
    units = _object(data.get("hourly_units"))
    for key, expected in (
        ("temperature_2m", "°C"),
        ("precipitation", "mm"),
        ("snowfall", "cm"),
        ("weather_code", "wmo code"),
    ):
        if units.get(key) != expected:
            raise ValueError("weather units differ")
    hourly = _object(data.get("hourly"))
    times, temps, rain, snow, codes = (
        _array(hourly.get(key), 72)
        for key in ("time", "temperature_2m", "precipitation", "snowfall", "weather_code")
    )
    if not times or any(len(items) != len(times) for items in (temps, rain, snow, codes)):
        raise ValueError("weather hours incomplete")
    hours: list[WeatherHour] = []
    for stamp, temp, wet, flake, code in zip(times, temps, rain, snow, codes, strict=True):
        if not isinstance(stamp, str) or len(stamp) != 16:
            raise ValueError("weather hour invalid")
        at = _time(stamp + "+00:00")
        if at.minute != 0 or hours and at != hours[-1].at + timedelta(hours=1):
            raise ValueError("weather hours unordered")
        if type(code) is not int or code not in (
            0,
            1,
            2,
            3,
            45,
            48,
            51,
            53,
            55,
            56,
            57,
            61,
            63,
            65,
            66,
            67,
            71,
            73,
            75,
            77,
            80,
            81,
            82,
            85,
            86,
            95,
            96,
            99,
        ):
            raise ValueError("weather code invalid")
        hours.append(
            WeatherHour(
                at,
                _number(temp, -80, 60),
                _number(wet, 0, 500),
                _number(flake, 0, 500),
                code,
            )
        )
    return Forecast(fetched_at, tuple(hours))


def _geometry(raw: object) -> tuple[Polygon, ...]:
    data = _object(raw)
    coordinates = _array(data.get("coordinates"))
    if data.get("type") == "Polygon":
        coordinates = [coordinates]
    elif data.get("type") != "MultiPolygon":
        raise ValueError("weather geometry unsupported")
    polygons = []
    for raw_polygon in coordinates:
        rings = []
        for raw_ring in _array(raw_polygon):
            points = []
            for pair in _array(raw_ring, 100_000):
                p = _array(pair, 2)
                if len(p) != 2:
                    raise ValueError("weather coordinate invalid")
                points.append((_number(p[0], -180, 180), _number(p[1], -90, 90)))
            if len(points) < 4 or points[0] != points[-1]:
                raise ValueError("weather polygon is not closed")
            rings.append(tuple(points))
        if not rings:
            raise ValueError("weather polygon empty")
        polygons.append(tuple(rings))
    if not polygons:
        raise ValueError("weather geometry empty")
    return tuple(polygons)


def _in_ring(point: Point, ring: Ring) -> bool:
    x, y = point
    inside = False
    for (ax, ay), (bx, by) in pairwise(ring):
        cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
        if (
            abs(cross) < 1e-10
            and min(ax, bx) <= x <= max(ax, bx)
            and min(ay, by) <= y <= max(ay, by)
        ):
            return True
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _in_polygon(point: Point, polygon: Polygon) -> bool:
    return _in_ring(point, polygon[0]) and not any(_in_ring(point, hole) for hole in polygon[1:])


def parse_warnings(raw: object, checked_at: datetime) -> WarningSnapshot:
    data = _object(raw)
    meta = _object(data.get("metadata"))
    generated = _time(meta.get("generated_at"))
    if (
        meta.get("schema_version") != "0.7"
        or meta.get("publication_type") not in ("manual", "heartbeat")
        or meta.get("domain", "public") != "public"
        or not timedelta(0) <= checked_at - generated < timedelta(hours=26)
        or generated < datetime(2026, 11, 2, tzinfo=UTC)
    ):
        raise ValueError("warning source unknown or expired")
    sets = _array(data.get("warnings"))
    if data.get("num_warning_sets") != len(sets):
        raise ValueError("warning set count differs")
    total = 0
    warnings = []
    colors = {"Moderate": "yellow", "Severe": "orange", "Extreme": "red"}
    for raw_set in sets:
        item = _object(raw_set)
        parts = _array(item.get("parts"))
        total += len(parts)
        if (
            item.get("operational_status", "Actual") != "Actual"
            or item.get("domain", "public") != "public"
            or item.get("status", "active") in ("cancelled", "expired")
            or item.get("warning_type") == "potentially-dangerous-weather"
        ):
            continue
        if item.get("status", "active") != "active" or item.get("warning_type") != "warning":
            raise ValueError("warning status unsupported")
        text = _object(_object(item.get("texts")).get("nl"))
        headline = text.get("headline")
        if not isinstance(headline, str) or not headline.strip() or len(headline) > 255:
            raise ValueError("warning headline missing")
        for raw_part in parts:
            part = _object(raw_part)
            if part.get("status", "active") in ("cancelled", "expired"):
                continue
            severity = part.get("severity")
            color = colors.get(severity) if isinstance(severity, str) else None
            if (
                color is None
                or part.get("color", color) not in (None, color)
                or part.get("status", "active") != "active"
            ):
                raise ValueError("warning severity unknown")
            onset = _time(part.get("onset", item.get("onset")))
            expires = _time(part.get("expires", item.get("expires")))
            if onset >= expires:
                raise ValueError("warning time range invalid")
            polygons = _geometry(part.get("area"))
            warnings.append(
                WeatherWarning(
                    cast(Color, color),
                    " ".join(headline.split()),
                    onset,
                    expires,
                    polygons,
                )
            )
    if data.get("num_warnings") != total:
        raise ValueError("warning count differs")
    return WarningSnapshot(generated, checked_at, tuple(warnings))


@dataclass(frozen=True, slots=True)
class _CachedForecast:
    position: tuple[float, float]
    tried_at: datetime
    value: Forecast | None


class WeatherProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        now: Callable[[], datetime],
        knmi_key: str | None = None,
    ) -> None:
        self._client, self._now, self._key = client, now, knmi_key
        self._forecasts: dict[str, _CachedForecast] = {}
        self._warnings: WarningSnapshot | None = None
        self._warnings_tried: datetime | None = None

    async def _json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        authenticated: bool = False,
    ) -> object:
        request = self._client.build_request("GET", url, params=params, timeout=5)
        # Explicit per-request credentials never reach Open-Meteo or a presigned download host.
        for header in ("Authorization", "Ocp-Apim-Subscription-Key", "Cookie"):
            request.headers.pop(header, None)
        if authenticated:
            request.headers["Authorization"] = self._key or ""
        async with asyncio.timeout(8):
            response = await self._client.send(
                request,
                stream=True,
                follow_redirects=False,
                auth=None,
            )
            try:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ValueError("weather response too large")
                return json.loads(body)
            finally:
                await response.aclose()

    async def refresh(self, stations: Mapping[str, tuple[float, float]]) -> None:
        selected = dict(list(stations.items())[:MAX_STATIONS])
        self._forecasts = {k: v for k, v in self._forecasts.items() if k in selected}
        for station, position in selected.items():
            now = self._now()
            prior = self._forecasts.get(station)
            ttl = FORECAST_TTL if prior and prior.value else RETRY_AFTER
            if prior and prior.position == position and timedelta(0) <= now - prior.tried_at < ttl:
                continue
            self._forecasts[station] = _CachedForecast(position, now, None)
            try:
                lat, lon = position
                _number(lat, -90, 90)
                _number(lon, -180, 180)
                raw = await self._json(
                    FORECAST_URL,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "models": "knmi_seamless",
                        "hourly": "temperature_2m,precipitation,snowfall,weather_code",
                        "forecast_days": 2,
                        "timezone": "UTC",
                    },
                )
                value = parse_forecast(raw, self._now())
                self._forecasts[station] = _CachedForecast(position, now, value)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError):
                pass  # Optional data stays unknown. Never log credentials or presigned URLs.
        if selected:
            await self._refresh_warnings()

    async def _refresh_warnings(self) -> None:
        now = self._now()
        if self._key is None or (
            self._warnings_tried is not None
            and timedelta(0) <= now - self._warnings_tried < WARNING_TTL
        ):
            return
        self._warnings_tried, self._warnings = now, None
        try:
            raw = _object(
                await self._json(
                    WARNINGS_URL,
                    authenticated=True,
                    params={
                        "maxKeys": 100,
                        "sorting": "desc",
                        "orderBy": "created",
                    },
                )
            )
            files = _array(raw.get("files"), 100)
            name = next(
                f["filename"]
                for f in map(_object, files)
                if isinstance(f.get("filename"), str) and f["filename"].endswith(".json")
            )
            link = _object(
                await self._json(
                    f"{WARNINGS_URL}/{quote(name, safe='')}/url",
                    authenticated=True,
                )
            ).get("temporaryDownloadUrl")
            if not isinstance(link, str):
                raise TypeError("warning download missing")
            parsed = urlsplit(link)
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or not host.endswith((".knmi.nl", ".amazonaws.com"))
            ):
                raise ValueError("warning download host unsupported")
            self._warnings = parse_warnings(await self._json(link), self._now())
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError, StopIteration):
            pass

    def snapshot(
        self,
        station: str,
        position: tuple[float, float],
        when: datetime,
        now: datetime,
    ) -> WeatherView:
        cached = self._forecasts.get(station)
        forecast = cached.value if cached is not None and cached.position == position else None
        hour = None
        if forecast is not None and timedelta(0) <= now - forecast.fetched_at < FORECAST_TTL:
            nearest = min(forecast.hours, key=lambda h: abs(h.at - when))
            if abs(nearest.at - when) <= timedelta(minutes=30):
                hour = nearest
        warnings = self._warnings
        if warnings is not None and not (
            timedelta(0) <= now - warnings.checked_at < WARNING_TTL
            and timedelta(0) <= now - warnings.generated_at < timedelta(hours=26)
        ):
            warnings = None
        return WeatherView(
            station,
            when,
            hour,
            forecast.fetched_at if hour and forecast else None,
            ()
            if warnings is None
            else tuple(
                WarningView(w.color, w.headline, w.onset, w.expires)
                for w in warnings.at(position, when)
            ),
            warnings.checked_at if warnings else None,
            warnings.generated_at if warnings else None,
        )


def weather_text(view: WeatherView) -> str:
    text = f"Weather at {view.station} around {hhmm(view.when)}: "
    if view.forecast is None:
        text += "forecast unavailable."
    else:
        hour = view.forecast
        text += (
            f"{hour.temperature_c:g}°C, {hour.precipitation_mm:g} mm precipitation, "
            f"{hour.snowfall_cm:g} cm snow (forecast hour {hhmm(hour.at)}). "
            + " ".join(hour.tips)
            + f" Retrieved {view.fetched_at.isoformat() if view.fetched_at else 'unknown'}. "
            "Forecast: Open-Meteo https://open-meteo.com/ (KNMI model); tips adapted by TCC. "
            "CC BY 4.0 https://creativecommons.org/licenses/by/4.0/."
        )
    if view.warnings_checked_at is None:
        return (
            text
            + " KNMI warnings unavailable; check https://www.knmi.nl/nederland-nu/weer/waarschuwingen/."
        )
    warnings = "; ".join(f"code {w.color}: {w.headline}" for w in view.warnings)
    return text + (
        f" KNMI: {warnings or 'no published warning for this station and time'}. "
        f"Checked {view.warnings_checked_at.isoformat()}; published {view.warnings_generated_at.isoformat() if view.warnings_generated_at else 'unknown'}. "
        "https://www.knmi.nl/nederland-nu/weer/waarschuwingen/ "
        "CC BY 4.0 https://creativecommons.org/licenses/by/4.0/."
    )

"""Strict mapping for the optional NS Virtual Train product."""

import math
import re
from datetime import datetime
from typing import Any, Literal

from train_commute_companion.calendar import TZ
from train_commute_companion.model import TrainId
from train_commute_companion.providers.base import ProviderError
from train_commute_companion.vehicle import (
    PlatformFeature,
    SeatCapacity,
    StockUnit,
    TrainContext,
    VehiclePosition,
    VehiclePositions,
)

BASE_URL = "https://gateway.apiportal.ns.nl/virtual-train-api"


class ContextUnavailable(ProviderError):
    """Optional vehicle context is unavailable; this says nothing about the main NS key."""


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContextUnavailable("NS vehicle context has an invalid object")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ContextUnavailable("NS vehicle context has an invalid list")
    return value


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ContextUnavailable("NS vehicle context has invalid text")
    return value


def _number(value: object, *, maximum: float | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ContextUnavailable("NS vehicle context has an invalid number")
    try:
        number = float(value)
    except OverflowError:
        raise ContextUnavailable("NS vehicle context has an invalid number") from None
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        raise ContextUnavailable("NS vehicle context has an invalid number")
    return number


def _count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise ContextUnavailable("NS vehicle context has an invalid count")
    return value


def _identifier(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ContextUnavailable("NS vehicle context has an invalid service identifier")
    return value


def coordinates(latitude: object, longitude: object) -> tuple[float, float]:
    values = []
    for raw, limit in ((latitude, 90), (longitude, 180)):
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise ContextUnavailable("vehicle coordinates are invalid")
        try:
            value = float(raw)
        except OverflowError:
            raise ContextUnavailable("vehicle coordinates are invalid") from None
        if not math.isfinite(value) or not -limit <= value <= limit:
            raise ContextUnavailable("vehicle coordinates are invalid")
        values.append(value)
    return values[0], values[1]


def context_request(train: TrainId, station: str, now: datetime) -> tuple[str, dict[str, str]]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContextUnavailable("vehicle context clock has no timezone")
    if train.operating_date != now.astimezone(TZ).date():
        raise ContextUnavailable("vehicle context supports only the current operating date")
    if train.planned_departure.tzinfo is None or train.planned_departure.utcoffset() is None:
        raise ContextUnavailable("vehicle service time has no timezone")
    if train.planned_departure.astimezone(TZ).date() != train.operating_date:
        raise ContextUnavailable("vehicle service date is inconsistent")
    number = _identifier(train.train_number)
    if not isinstance(station, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", station):
        raise ContextUnavailable("vehicle context requires a station code")
    return f"/v1/trein/{number}/{station}", {
        "dateTime": now.isoformat(timespec="seconds"),
        "features": "zitplaats,platformitems,cta,drukte,druktev2",
    }


def _capacity(value: object) -> SeatCapacity:
    raw = {} if value is None else _object(value)
    return SeatCapacity(
        *(
            _count(raw.get(key))
            for key in (
                "zitplaatsEersteKlas",
                "zitplaatsTweedeKlas",
                "klapstoelEersteKlas",
                "klapstoelTweedeKlas",
                "fietsplekken",
            )
        )
    )


def _unit(value: object) -> StockUnit:
    raw = _object(value)
    number = raw.get("materieelnummer")
    equipment = raw.get("faciliteiten")
    items = None if equipment is None else tuple(_text(x) for x in _list(equipment))
    if items is not None and any(x is None for x in items):
        raise ContextUnavailable("NS vehicle equipment is invalid")
    return StockUnit(
        None if number is None else _identifier(number),
        _text(raw.get("type")),
        None if items is None else tuple(x for x in items if x is not None),
        _capacity(raw.get("zitplaatsen")),
    )


def _feature(value: object) -> PlatformFeature:
    raw = _object(value)
    kind = _text(raw.get("type"))
    if kind is None:
        raise ContextUnavailable("NS platform feature has no type")
    description = raw.get("description")
    return PlatformFeature(kind, None if description == "" else _text(description))


def map_train_context(
    raw: object, train: TrainId, station: str, fetched_at: datetime
) -> TrainContext:
    context_request(train, station, fetched_at)
    item = _object(raw)
    if _identifier(item.get("ritnummer")) != train.train_number or item.get("station") != station:
        raise ContextUnavailable(
            "NS vehicle context does not match the requested train and station"
        )
    shortened = item.get("ingekort")
    if shortened is not None and not isinstance(shortened, bool):
        raise ContextUnavailable("NS shortened-train status is invalid")
    units, features = item.get("materieeldelen"), item.get("perronVoorzieningen")
    directions: dict[str, Literal["left", "right"]] = {"LINKS": "left", "RECHTS": "right"}
    direction = directions.get(_text(item.get("rijrichting")) or "")
    return TrainContext(
        train,
        station,
        fetched_at,
        _text(item.get("bron")),
        _text(item.get("type")),
        _text(item.get("vervoerder")),
        _text(item.get("spoor")),
        shortened,
        _count(item.get("lengte")),
        _number(item.get("lengteInMeters")),
        None if units is None else tuple(_unit(x) for x in _list(units)),
        None if features is None else tuple(_feature(x) for x in _list(features)),
        direction,
    )


def map_positions(raw: object, fetched_at: datetime) -> VehiclePositions:
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ContextUnavailable("vehicle context clock has no timezone")
    items = _list(_object(_object(raw).get("payload")).get("treinen"))
    positions = []
    seen = set()
    for value in items:
        item = _object(value)
        number = _identifier(item.get("treinNummer"))
        if number in seen:
            raise ContextUnavailable("NS vehicle positions have ambiguous service identities")
        seen.add(number)
        lat, lon = coordinates(item.get("lat"), item.get("lng"))
        positions.append(
            VehiclePosition(
                number,
                _text(item.get("ritId")),
                lat,
                lon,
                _text(item.get("bron")),
                _text(item.get("type")),
                _number(item.get("snelheid")),
                _number(item.get("richting"), maximum=360),
                _number(item.get("horizontaleNauwkeurigheid")),
            )
        )
    return VehiclePositions(fetched_at, tuple(positions))

"""NS Reisinformatie mapping. The only place that knows NS field names."""

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, get_args
from urllib.parse import urlsplit

import httpx

from train_commute_companion.calendar import TZ, has_room
from train_commute_companion.location import number
from train_commute_companion.model import (
    Connection,
    CrowdForecast,
    Leg,
    Provenance,
    Stop,
    TimeObs,
    TrainId,
    TransportMode,
)
from train_commute_companion.providers.base import (
    ActiveDisruption,
    ActiveDisruptions,
    AuthError,
    DoorToDoorNotAllowed,
    MalformedAnswer,
    PlannedWork,
    PlannedWorks,
    ProviderAnswer,
    ProviderError,
    Query,
    RateLimited,
    ServerError,
)
from train_commute_companion.providers.ns_vehicle import (
    BASE_URL as VEHICLE_BASE_URL,
)
from train_commute_companion.providers.ns_vehicle import (
    ContextUnavailable,
    context_request,
    coordinates,
    map_positions,
    map_train_context,
)
from train_commute_companion.vehicle import TrainContext, VehiclePositions


def _dt(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
        try:
            t = datetime.strptime(v, fmt).astimezone(TZ)
            return t if has_room(t) else None  # the store would refuse it, and lose the whole state
        except (ValueError, OverflowError):  # Overflow: at the calendar's edge, past it in our zone
            pass
    return None


def _obs(planned: datetime | None, actual: datetime | None, at: datetime) -> TimeObs | None:
    # a reported time stands on its own, with or without a planned one (7.12 rule 20)
    if actual is not None:
        return TimeObs(planned, actual, Provenance.REPORTED, at)
    if planned is not None:
        return TimeObs(planned, None, Provenance.PLANNED, at)
    return None


def _str(v: object) -> str | None:
    return str(v) if v is not None else None


def _uic(raw: dict[str, Any]) -> str | None:
    return str(raw["uicCode"]) if raw.get("uicCode") else None


def _place_id(raw: dict[str, Any]) -> str | None:
    lat, lon, name = number(raw.get("lat")), number(raw.get("lng")), raw.get("name")
    if (
        lat is None
        or lon is None
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
        or not isinstance(name, str)
        or not name.strip()
    ):
        return None
    # Non-train addresses need stable identity without pretending to be NS stations.
    place = json.dumps([lat + 0.0, lon + 0.0, name], ensure_ascii=True)
    return "@place:" + hashlib.sha256(place.encode()).hexdigest()


def _physical_fields(raw: dict[str, Any]) -> dict[str, Any]:
    physical_id = _place_id(raw)
    return {
        "physical_id": physical_id,
        "latitude": number(raw.get("lat")) if physical_id else None,
        "longitude": number(raw.get("lng")) if physical_id else None,
    }


def _ids(
    raw: dict[str, Any], aliases: dict[str, str], *, allow_place: bool = False
) -> tuple[str | None, str | None]:
    """(code, uic), each filled from the trip's aliases when the stop carries only the other."""
    uic, code = _uic(raw), _str(raw.get("stationCode") or None)
    uic = uic or (aliases.get(code) if code else None)
    code = code or (aliases.get(uic, uic) if uic else None)
    if code is None and allow_place:
        code = _place_id(raw)
    return code, uic


def _accessibility_notes(raw: dict[str, Any]) -> tuple[str, ...]:
    notes = raw.get("notes")
    if not isinstance(notes, list):
        return ()
    return tuple(
        dict.fromkeys(
            note["value"].strip()
            for note in notes
            if isinstance(note, dict)
            and note.get("key") == "SS"
            and note.get("noteType") == "ATTRIBUTE"
            and isinstance(note.get("value"), str)
            and note["value"].strip()
        )
    )


def _exit_side(raw: dict[str, Any]) -> Literal["left", "right"] | None:
    side = raw.get("exitSide")
    return "left" if side == "LEFT" else "right" if side == "RIGHT" else None


def _stop(raw: dict[str, Any], aliases: dict[str, str], at: datetime, *, allow_place: bool) -> Stop:
    code, uic = _ids(raw, aliases, allow_place=allow_place)
    if not code:
        raise MalformedAnswer("stop has no station identifier")
    pa, pd = _dt(raw.get("plannedArrivalDateTime")), _dt(raw.get("plannedDepartureDateTime"))
    return Stop(
        code=str(code),
        name=str(raw.get("name", code)),
        planned_arrival=pa,
        planned_departure=pd,
        arrival=_obs(pa, _dt(raw.get("actualArrivalDateTime")), at),
        departure=_obs(pd, _dt(raw.get("actualDepartureDateTime")), at),
        cancelled=bool(raw.get("cancelled", False)),
        passing=bool(raw.get("passing", False)),
        # each side on its own, so a live arrival track never reads as a moved departure track
        planned_track=_str(raw.get("plannedDepartureTrack") or None),
        actual_track=_str(raw.get("actualDepartureTrack") or None),
        uic=uic,
        accessibility_notes=_accessibility_notes(raw),
        exit_side=_exit_side(raw),
        planned_arrival_track=_str(raw.get("plannedArrivalTrack") or None),
        actual_arrival_track=_str(raw.get("actualArrivalTrack") or None),
        **_physical_fields(raw),
    )


def _end_stop(
    raw: dict[str, Any], aliases: dict[str, str], *, depart: bool, at: datetime, allow_place: bool
) -> Stop:
    """Boarding or alighting stop built from a leg's origin or destination."""
    p, a = _dt(raw.get("plannedDateTime")), _dt(raw.get("actualDateTime"))
    code, uic = _ids(raw, aliases, allow_place=allow_place)
    if not code:
        raise MalformedAnswer("leg end has no station identifier")
    o = _obs(p, a, at)
    return Stop(
        code=code,
        name=str(raw.get("name", code)),
        planned_arrival=None if depart else p,
        planned_departure=p if depart else None,
        arrival=None if depart else o,
        departure=o if depart else None,
        cancelled=bool(raw.get("cancelled", False)),
        passing=False,
        planned_track=_str(raw.get("plannedTrack")) if depart else None,
        actual_track=_str(raw.get("actualTrack")) if depart else None,
        uic=uic,
        accessibility_notes=_accessibility_notes(raw),
        exit_side=_exit_side(raw),
        planned_arrival_track=None if depart else _str(raw.get("plannedTrack")),
        actual_arrival_track=None if depart else _str(raw.get("actualTrack")),
        **_physical_fields(raw),
    )


def _int_in(text: object) -> int | None:
    # a longer run is no real number of minutes, and int() refuses one over its digit limit
    m = re.search(r"(?<!\d)\d{1,9}(?!\d)", text) if isinstance(text, str) else None
    return int(m.group()) if m else None


def _objects(v: object, what: str) -> list[Any]:
    """An optional list of objects: absent is empty, any other shape is malformed."""
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(m, dict) for m in v):
        raise MalformedAnswer(f"leg {what} is not a list of objects")
    return v


def _number(v: object) -> str | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return v if isinstance(v, str) and v.strip() else None


def _leg_stops(
    raw: dict[str, Any], aliases: dict[str, str], at: datetime, *, allow_place: bool
) -> tuple[Stop, ...]:
    origin, dest, stops_raw = raw.get("origin"), raw.get("destination"), raw.get("stops")
    if stops_raw is not None and stops_raw != []:
        # a present but broken stop list may hide a cancelled stop: never fall back over it
        if len(_objects(stops_raw, "stops")) < 2:
            raise MalformedAnswer("leg stop list has fewer than two stops")
        stops = tuple(_stop(s, aliases, at, allow_place=allow_place) for s in stops_raw)
        # Endpoint notes belong to their matching stop, never to a truncated endpoint.
        for index, endpoint in ((0, origin), (-1, dest)):
            if isinstance(endpoint, dict):
                code, _ = _ids(endpoint, aliases, allow_place=allow_place)
                if code == stops[index].code:
                    sides = {
                        side for side in (_exit_side(endpoint), stops[index].exit_side) if side
                    }
                    physical = _physical_fields(endpoint)
                    name = stops[index].name
                    if physical["physical_id"] is not None:
                        name = endpoint["name"]
                    if stops[index].physical_id is not None:
                        if physical["physical_id"] is None:
                            physical = {
                                "physical_id": stops[index].physical_id,
                                "latitude": stops[index].latitude,
                                "longitude": stops[index].longitude,
                            }
                        elif physical["physical_id"] != stops[index].physical_id:
                            physical = dict.fromkeys(physical)
                            name = stops[index].name
                    stop = replace(
                        stops[index],
                        name=name,
                        accessibility_notes=tuple(
                            dict.fromkeys(
                                (*stops[index].accessibility_notes, *_accessibility_notes(endpoint))
                            )
                        ),
                        exit_side=next(iter(sides)) if len(sides) == 1 else None,
                        **physical,
                    )
                    items = list(stops)
                    items[index] = stop
                    stops = tuple(items)
        return stops
    if isinstance(origin, dict) and isinstance(dest, dict):
        return (
            _end_stop(origin, aliases, depart=True, at=at, allow_place=allow_place),
            _end_stop(dest, aliases, depart=False, at=at, allow_place=allow_place),
        )
    raise MalformedAnswer("leg has no stops and no origin/destination")


def _leg(raw: object, aliases: dict[str, str], at: datetime) -> Leg:
    if not isinstance(raw, dict):
        raise MalformedAnswer("leg is not an object")
    prod = raw.get("product")
    prod = prod if isinstance(prod, dict) else {}
    is_train = prod.get("type") == "TRAIN"
    stops = _leg_stops(
        raw, aliases, at, allow_place=prod.get("type") in ("BUS", "METRO", "TRAM", "FERRY", "WALK")
    )
    planned_dep = stops[0].planned_departure
    if planned_dep is None:
        raise MalformedAnswer("leg has no planned departure at its boarding stop")
    number = _number(prod.get("number"))
    if is_train and number is None:
        raise MalformedAnswer("train leg has no usable train number")
    tm = [
        _int_in(m.get("message"))
        for m in _objects(raw.get("transferMessages"), "transferMessages")
        if m.get("type") == "TRANSFER_TIME"
    ]
    tt, cp = raw.get("transferTimeToNextLeg"), raw.get("crossPlatformTransfer")
    msgs = tuple(
        str(m.get("text") or m.get("message"))
        for m in _objects(raw.get("messages"), "messages")
        if m.get("text") or m.get("message")
    )
    return Leg(
        train=TrainId(planned_dep.date(), number or "", planned_dep),
        category=str(prod.get("categoryCode", "")),
        stops=stops,
        cancelled=bool(raw.get("cancelled", False)),
        part_cancelled=bool(raw.get("partCancelled", False)),
        change_possible=_opt_bool(raw.get("changePossible")),
        reachable=_opt_bool(raw.get("reachable")),
        alternative_transport=not is_train,
        messages=msgs,
        transfer_time_to_next=tt if isinstance(tt, int) and not isinstance(tt, bool) else None,
        transfer_message_min=next((m for m in tm if m is not None), None),
        cross_platform=_opt_bool(cp),
        crowd_forecast=_crowd_forecast(raw.get("crowdForecast")),
        transport_mode=next(
            (mode for mode in get_args(TransportMode) if mode == prod.get("type")), None
        ),
        operator=prod.get("operatorName") if isinstance(prod.get("operatorName"), str) else None,
        service_reference=_number(prod.get("number")),
    )


def _crowd_forecast(raw: object) -> CrowdForecast | None:
    levels: dict[str, CrowdForecast] = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
    return levels.get(raw) if isinstance(raw, str) else None


def _opt_bool(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


def _aliases(legs_raw: list[Any]) -> dict[str, str]:
    """uic -> station code and station code -> uic, from every place in the trip naming both."""
    out: dict[str, str] = {}
    for lr in legs_raw:
        if not isinstance(lr, dict):
            continue
        stops = lr.get("stops")
        stops = stops if isinstance(stops, list) else []
        for p in (lr.get("origin"), lr.get("destination"), *stops):
            if isinstance(p, dict) and p.get("uicCode") and p.get("stationCode"):
                uic, code = str(p["uicCode"]), str(p["stationCode"])
                out[uic], out[code] = code, uic
    return out


def map_trips(payload: object, q: Query, fetched_at: datetime) -> ProviderAnswer:
    if not isinstance(payload, dict) or not isinstance(payload.get("trips"), list):
        raise MalformedAnswer("answer has no trips list")
    conns: list[Connection] = []
    for trip in payload["trips"]:
        legs_raw = trip.get("legs") if isinstance(trip, dict) else None
        if not isinstance(legs_raw, list) or not legs_raw:
            raise MalformedAnswer("trip has no legs")
        legs = tuple(_leg(lr, _aliases(legs_raw), fetched_at) for lr in legs_raw)
        ctx = trip.get("ctxRecon")
        conns.append(
            Connection(
                legs, str(trip.get("status", "NORMAL")), ctx if isinstance(ctx, str) else None
            )
        )
    return ProviderAnswer(q, tuple(conns), fetched_at)


def _items(payload: object) -> list[Any] | None:
    if isinstance(payload, dict):
        payload = payload.get("payload")
    return payload if isinstance(payload, list) else None


def _station_ids(s: object) -> set[str] | None:
    # uicCode is required: trip stops always carry it, intermediate stops often nothing else
    if not isinstance(s, dict) or not s.get("uicCode"):
        return None
    return {str(s[k]) for k in ("uicCode", "stationCode") if s.get(k)}


def _section_stations(ps: object) -> set[str] | None:
    """The stations of one publication section; None when any part of it is unreadable."""
    if not isinstance(ps, dict):
        return None
    cons = ps.get("consequence")
    if cons is not None and not isinstance(cons, dict):
        return None
    parts = [p for p in (ps.get("section"), (cons or {}).get("section")) if p is not None]
    if not parts:
        return None
    stations: set[str] = set()
    for part in parts:
        st = part.get("stations") if isinstance(part, dict) else None
        if not isinstance(st, list) or not st:
            return None
        for raw in st:
            ids = _station_ids(raw)
            if ids is None:
                return None
            stations |= ids
    return stations


def _work(d: dict[str, Any]) -> PlannedWork | None:
    """One MAINTENANCE item, or None when it cannot be evaluated."""
    start, end = _dt(d.get("start")), _dt(d.get("end"))
    if start is None or end is None:
        return None
    # UTC: aware datetimes sharing one zone compare by wall clock, wrong in the repeated hour
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start:
        return None
    sections = d.get("publicationSections")
    if not isinstance(sections, list) or not sections:
        return None
    stations: set[str] = set()
    for ps in sections:
        ids = _section_stations(ps)
        if ids is None:
            return None
        stations |= ids
    return PlannedWork(start, end, frozenset(stations), str(d.get("title", "")))


def map_disruptions(payload: object) -> PlannedWorks:
    """Planned works only. Any unreadable item makes coverage unknown (no probe may call itself clean)."""
    raw = _items(payload)
    if raw is None:
        return PlannedWorks((), "unknown")
    items: list[PlannedWork] = []
    complete = True
    for d in raw:
        kind = d.get("type") if isinstance(d, dict) else None
        if not isinstance(kind, str) or not kind:
            complete = False
            continue
        if kind != "MAINTENANCE":
            continue
        w = _work(d)
        if w is None:
            complete = False
        else:
            items.append(w)
    return PlannedWorks(tuple(items), "complete" if complete else "unknown")


def _active_disruption(raw: object) -> ActiveDisruption | None:
    if not isinstance(raw, dict):
        raise MalformedAnswer("incident is not an object")
    if raw.get("isActive") is False:
        return None
    ident, title, kind = raw.get("id"), raw.get("title"), raw.get("type")
    if raw.get("isActive") is not True or not all(
        isinstance(x, str) and x.strip() for x in (ident, title, kind)
    ):
        raise ValueError("incident identity or active status is unknown")
    stations: set[str] = set()
    sections = raw.get("publicationSections", [])
    for section in _objects(sections, "publicationSections"):
        parts = [section.get("section")]
        consequence = section.get("consequence")
        if consequence is not None:
            if not isinstance(consequence, dict):
                raise ValueError("incident consequence is unreadable")
            parts.append(consequence.get("section"))
        for part in (p for p in parts if p is not None):
            if not isinstance(part, dict):
                raise MalformedAnswer("incident section is unreadable")
            for station in _objects(part.get("stations"), "stations"):
                ids = [station.get(k) for k in ("stationCode", "uicCode")]
                valid = [
                    str(x)
                    for x in ids
                    if isinstance(x, str | int) and not isinstance(x, bool) and str(x).strip()
                ]
                if not valid:
                    raise ValueError("incident station has no identity")
                stations.update(valid)
    link = raw.get("url")
    safe_url = None
    if isinstance(link, str):
        try:
            url = urlsplit(link)
            if (
                url.scheme == "https"
                and url.hostname in ("ns.nl", "www.ns.nl")
                and not url.username
                and not url.password
                and url.port in (None, 443)
            ):
                safe_url = link
        except ValueError:
            pass
    assert isinstance(ident, str) and isinstance(title, str)
    return ActiveDisruption(ident, " ".join(title.split())[:500], frozenset(stations), safe_url)


def map_active_disruptions(payload: object) -> ActiveDisruptions:
    raw = _items(payload)
    if raw is None:
        return ActiveDisruptions((), "unknown")
    items: dict[str, ActiveDisruption] = {}
    conflicts: set[str] = set()
    complete = True
    for value in raw:
        try:
            item = _active_disruption(value)
        except (ValueError, MalformedAnswer):
            complete = False
            continue
        if item is None or item.id in conflicts:
            continue
        if item.id in items and items[item.id] != item:
            conflicts.add(item.id)
            items.pop(item.id)
            complete = False
        else:
            items[item.id] = item
    return ActiveDisruptions(tuple(items.values()), "complete" if complete else "unknown")


def map_stations(payload: object) -> frozenset[str]:
    raw = _items(payload)
    if raw is None:
        raise MalformedAnswer("answer has no station list")
    codes = {
        str(s.get("code") or s.get("stationCode"))
        for s in raw
        if isinstance(s, dict) and (s.get("code") or s.get("stationCode"))
    }
    if not codes:
        raise MalformedAnswer("station list is empty")
    return frozenset(codes)


def map_station_positions(payload: object) -> dict[str, tuple[float, float]]:
    """Code to (lat, lng) for stations with numeric coordinates; never raises."""
    return {
        str(s.get("code") or s.get("stationCode")): (float(s["lat"]), float(s["lng"]))
        for s in _items(payload) or ()
        if isinstance(s, dict)
        and (s.get("code") or s.get("stationCode"))
        and number(s.get("lat")) is not None
        and number(s.get("lng")) is not None
    }


def map_station_names(payload: object) -> dict[str, str]:
    """Station codes and their full display names, with codes as the safe fallback."""
    codes = map_stations(payload)
    names = {code: code for code in codes}
    for station in _items(payload) or ():
        if not isinstance(station, dict):
            continue
        code = str(station.get("code") or station.get("stationCode") or "")
        labels = station.get("namen")
        label = labels.get("lang") if isinstance(labels, dict) else None
        if code in names and isinstance(label, str) and label.strip():
            names[code] = label
    return names


BASE_URL = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"


# Hard cap per request (7.1); the runtime's per-poll deadline wraps whole polls.
REQUEST_DEADLINE_S = 90.0
VEHICLE_BODY_LIMIT = 2_000_000


def _raise_for_status(r: httpx.Response) -> None:
    code = r.status_code
    if code in (401, 403):
        raise AuthError(f"NS rejected the API key (HTTP {code})")
    if code == 429:
        ra = r.headers.get("Retry-After", "").strip()
        # a longer value is no real wait, and int() refuses one over its digit limit
        seconds = int(ra) if ra.isascii() and ra.isdigit() and len(ra) <= 9 else None
        raise RateLimited("NS rate limited", seconds)
    if code >= 500:
        raise ServerError(f"NS answered HTTP {code}")
    if code >= 400:
        try:
            payload = r.json()
        except (ValueError, RecursionError):
            payload = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list) and any(
            isinstance(error, dict)
            and error.get("type") == "API_KEY_NOT_ALLOWED_TO_PLAN_DOOR_TO_DOOR"
            for error in errors
        ):
            raise DoorToDoorNotAllowed("NS API key does not permit door-to-door journey planning")
        raise ProviderError(f"NS answered HTTP {code}")


class NsProvider:
    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime],
        deadline: float = REQUEST_DEADLINE_S,
    ) -> None:
        self._deadline = deadline
        self._key = api_key
        self._client = client
        self._clock = clock
        self.positions: dict[str, tuple[float, float]] = {}

    async def station_names(self) -> dict[str, str]:
        return map_station_names(await self._get("/v2/stations"))

    async def _get(
        self, path: str, params: dict[str, str] | None = None, *, optional_context: bool = False
    ) -> object:
        # Messages are fixed text: the key and the request URL never enter an exception.
        if not self._key.isascii():  # no header can carry it, so NS can never accept it
            if optional_context:
                raise ContextUnavailable("NS vehicle context credentials are unavailable")
            raise AuthError("the NS API key has non-ASCII characters")
        try:
            async with asyncio.timeout(self._deadline):
                if optional_context:
                    return await self._vehicle_json(path, params)
                r = await self._client.get(
                    BASE_URL + path,
                    params=params,
                    headers={"Ocp-Apim-Subscription-Key": self._key},
                    follow_redirects=False,
                )
        except TimeoutError:
            raise ProviderError("NS request exceeded the deadline") from None
        except httpx.HTTPError as e:
            raise ProviderError(f"NS request failed: {type(e).__name__}") from None
        _raise_for_status(r)
        try:
            return r.json()
        except (ValueError, RecursionError):  # Recursion: nested deeper than the parser goes
            raise MalformedAnswer("NS answer is not valid JSON") from None

    async def _vehicle_json(self, path: str, params: dict[str, str] | None) -> object:
        """Only fixed optional-product paths reach this bounded, non-redirecting request."""
        async with self._client.stream(
            "GET",
            VEHICLE_BASE_URL + path,
            params=params,
            headers={"Ocp-Apim-Subscription-Key": self._key},
            follow_redirects=False,
        ) as response:
            if response.status_code == 429:
                _raise_for_status(response)
            if not 200 <= response.status_code < 300:
                raise ContextUnavailable("NS vehicle context is unavailable")
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                content.extend(chunk)
                if len(content) > VEHICLE_BODY_LIMIT:
                    raise ContextUnavailable("NS vehicle context exceeds the response limit")
        try:
            return json.loads(content)
        except (ValueError, RecursionError):
            raise ContextUnavailable("NS vehicle context is not valid JSON") from None

    async def train_context(self, train: TrainId, station: str) -> TrainContext:
        """One optional-product request, scoped to today's service and boarding station."""
        path, params = context_request(train, station, self._clock())
        raw = await self._get(path, params, optional_context=True)
        fetched = self._clock()
        context_request(train, station, fetched)  # refuse a request that crossed local midnight
        return map_train_context(raw, train, station, fetched)

    async def vehicle_positions(
        self, latitude: float | None = None, longitude: float | None = None
    ) -> VehiclePositions:
        """One optional-product request, global or around a public station, without service binding."""
        params = None
        if latitude is not None or longitude is not None:
            lat, lon = coordinates(latitude, longitude)
            params = {"lat": str(lat), "lng": str(lon), "radius": "10000", "limit": "100"}
        raw = await self._get("/vehicle", params, optional_context=True)
        return map_positions(raw, self._clock())

    async def trips(self, q: Query) -> ProviderAnswer:
        if any(
            station is not None and (station.startswith("@place:") or station == "8400002")
            for station in (q.origin, q.destination, q.via)
        ):
            raise ProviderError("non-station locations cannot be used in a station journey query")
        params = {
            "fromStation": q.origin,
            "toStation": q.destination,
            "dateTime": q.at.isoformat(timespec="seconds"),
        }
        if q.require_accessible:
            params["searchForAccessibleTrip"] = "true"
        if q.via:
            params["viaStation"] = q.via
        payload = await self._get("/v3/trips", params)
        return map_trips(payload, q, self._clock())

    async def planned_works(self) -> PlannedWorks:
        # request failures raise (429/5xx feed shared backoff, 7.1); the caller treats them as unknown
        try:
            payload = await self._get("/v3/disruptions", {"isActive": "false"})
        except MalformedAnswer:  # _get raises it only for an undecodable 200 body
            return PlannedWorks((), "unknown")
        return map_disruptions(payload)

    async def active_disruptions(self) -> ActiveDisruptions:
        try:
            payload = await self._get("/v3/disruptions", {"isActive": "true"})
        except MalformedAnswer:
            return ActiveDisruptions((), "unknown")
        return map_active_disruptions(payload)

    async def stations(self) -> frozenset[str]:
        payload = await self._get("/v2/stations")
        codes = map_stations(payload)
        self.positions = map_station_positions(payload)  # same payload, no extra request
        return codes

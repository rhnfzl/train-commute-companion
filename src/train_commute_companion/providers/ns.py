"""NS Reisinformatie mapping. The only place that knows NS field names."""

import asyncio
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from train_commute_companion.model import (
    Connection,
    Leg,
    Provenance,
    Stop,
    TimeObs,
    TrainId,
)
from train_commute_companion.providers.base import (
    AuthError,
    MalformedAnswer,
    PlannedWork,
    PlannedWorks,
    ProviderAnswer,
    ProviderError,
    Query,
    RateLimited,
    ServerError,
)

AMS = ZoneInfo("Europe/Amsterdam")


def _dt(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
        try:
            return datetime.strptime(v, fmt).astimezone(AMS)
        except ValueError:
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


def _ids(raw: dict[str, Any], aliases: dict[str, str]) -> tuple[str | None, str | None]:
    """(code, uic), each filled from the trip's aliases when the stop carries only the other."""
    uic, code = _uic(raw), _str(raw.get("stationCode") or None)
    uic = uic or (aliases.get(code) if code else None)
    code = code or (aliases.get(uic, uic) if uic else None)
    return code, uic


def _stop(raw: dict[str, Any], aliases: dict[str, str], at: datetime) -> Stop:
    code, uic = _ids(raw, aliases)
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
        planned_arrival_track=_str(raw.get("plannedArrivalTrack") or None),
        actual_arrival_track=_str(raw.get("actualArrivalTrack") or None),
    )


def _end_stop(raw: dict[str, Any], aliases: dict[str, str], *, depart: bool, at: datetime) -> Stop:
    """Boarding or alighting stop built from a leg's origin or destination."""
    p, a = _dt(raw.get("plannedDateTime")), _dt(raw.get("actualDateTime"))
    code, uic = _ids(raw, aliases)
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
        planned_arrival_track=None if depart else _str(raw.get("plannedTrack")),
        actual_arrival_track=None if depart else _str(raw.get("actualTrack")),
    )


def _int_in(text: object) -> int | None:
    m = re.search(r"\d+", text) if isinstance(text, str) else None
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


def _leg(raw: object, aliases: dict[str, str], at: datetime) -> Leg:
    if not isinstance(raw, dict):
        raise MalformedAnswer("leg is not an object")
    origin, dest, stops_raw = raw.get("origin"), raw.get("destination"), raw.get("stops")
    if stops_raw is not None and stops_raw != []:
        # a present but broken stop list may hide a cancelled stop: never fall back over it
        if len(_objects(stops_raw, "stops")) < 2:
            raise MalformedAnswer("leg stop list has fewer than two stops")
        stops = tuple(_stop(s, aliases, at) for s in stops_raw)
    elif isinstance(origin, dict) and isinstance(dest, dict):
        stops = (
            _end_stop(origin, aliases, depart=True, at=at),
            _end_stop(dest, aliases, depart=False, at=at),
        )
    else:
        raise MalformedAnswer("leg has no stops and no origin/destination")
    planned_dep = stops[0].planned_departure
    if planned_dep is None:
        raise MalformedAnswer("leg has no planned departure at its boarding stop")
    prod = raw.get("product")
    prod = prod if isinstance(prod, dict) else {}
    is_train = prod.get("type") == "TRAIN"
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
    )


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
        if not isinstance(ps, dict):
            return None
        cons = ps.get("consequence")
        if cons is not None and not isinstance(cons, dict):
            return None
        parts = [p for p in (ps.get("section"), (cons or {}).get("section")) if p is not None]
        if not parts:
            return None
        for part in parts:
            st = part.get("stations") if isinstance(part, dict) else None
            if not isinstance(st, list) or not st:
                return None
            for raw in st:
                ids = _station_ids(raw)
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


def _num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def map_station_positions(payload: object) -> dict[str, tuple[float, float]]:
    """Code to (lat, lng) for stations with numeric coordinates; never raises."""
    return {
        str(s.get("code") or s.get("stationCode")): (float(s["lat"]), float(s["lng"]))
        for s in _items(payload) or ()
        if isinstance(s, dict)
        and (s.get("code") or s.get("stationCode"))
        and _num(s.get("lat"))
        and _num(s.get("lng"))
    }


BASE_URL = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"


# Hard cap per request (7.1); the runtime's per-poll deadline (plan Task 18) wraps whole polls.
REQUEST_DEADLINE_S = 90.0


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

    async def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        # Messages are fixed text: the key and the request URL never enter an exception.
        try:
            async with asyncio.timeout(self._deadline):
                r = await self._client.get(
                    BASE_URL + path,
                    params=params,
                    headers={"Ocp-Apim-Subscription-Key": self._key},
                )
        except TimeoutError:
            raise ProviderError("NS request exceeded the deadline") from None
        except httpx.HTTPError as e:
            raise ProviderError(f"NS request failed: {type(e).__name__}") from None
        code = r.status_code
        if code in (401, 403):
            raise AuthError(f"NS rejected the API key (HTTP {code})")
        if code == 429:
            ra = r.headers.get("Retry-After", "").strip()
            raise RateLimited("NS rate limited", int(ra) if ra.isdigit() else None)
        if code >= 500:
            raise ServerError(f"NS answered HTTP {code}")
        if code >= 400:
            raise ProviderError(f"NS answered HTTP {code}")
        try:
            return r.json()
        except ValueError:
            raise MalformedAnswer("NS answer is not valid JSON") from None

    async def trips(self, q: Query) -> ProviderAnswer:
        params = {
            "fromStation": q.origin,
            "toStation": q.destination,
            "dateTime": q.at.isoformat(timespec="seconds"),
        }
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

    async def stations(self) -> frozenset[str]:
        payload = await self._get("/v2/stations")
        codes = map_stations(payload)
        self.positions = map_station_positions(payload)  # same payload, no extra request
        return codes

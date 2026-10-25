"""NS Reisinformatie mapping. The only place that knows NS field names."""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from train_commute_companion.model import (
    Connection,
    Leg,
    Provenance,
    Stop,
    TimeObs,
    TrainId,
)
from train_commute_companion.providers.base import (
    MalformedAnswer,
    PlannedWork,
    PlannedWorks,
    ProviderAnswer,
    Query,
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
    if planned is None:
        return None
    if actual is not None:
        return TimeObs(planned, actual, Provenance.REPORTED, at)
    return TimeObs(planned, None, Provenance.PLANNED, at)


def _str(v: object) -> str | None:
    return str(v) if v is not None else None


def _stop(raw: dict[str, Any], codes: dict[str, str], at: datetime) -> Stop:
    uic = str(raw.get("uicCode", ""))
    code = raw.get("stationCode") or codes.get(uic, uic)
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
        planned_track=_str(raw.get("plannedDepartureTrack") or raw.get("plannedArrivalTrack")),
        actual_track=_str(raw.get("actualDepartureTrack") or raw.get("actualArrivalTrack")),
    )


def _end_stop(raw: dict[str, Any], *, depart: bool, at: datetime) -> Stop:
    """Boarding or alighting stop built from a leg's origin or destination."""
    p, a = _dt(raw.get("plannedDateTime")), _dt(raw.get("actualDateTime"))
    code = str(raw.get("stationCode") or raw.get("uicCode") or "")
    o = _obs(p, a, at)
    return Stop(
        code=code,
        name=str(raw.get("name", code)),
        planned_arrival=None if depart else p,
        planned_departure=p if depart else None,
        arrival=None if depart else o,
        departure=o if depart else None,
        cancelled=False,
        passing=False,
        planned_track=_str(raw.get("plannedTrack")),
        actual_track=_str(raw.get("actualTrack")),
    )


def _int_in(text: object) -> int | None:
    m = re.search(r"\d+", text) if isinstance(text, str) else None
    return int(m.group()) if m else None


def _leg(raw: object, codes: dict[str, str], at: datetime) -> Leg:
    if not isinstance(raw, dict):
        raise MalformedAnswer("leg is not an object")
    origin, dest, stops_raw = raw.get("origin"), raw.get("destination"), raw.get("stops")
    if isinstance(stops_raw, list) and stops_raw and all(isinstance(s, dict) for s in stops_raw):
        stops = tuple(_stop(s, codes, at) for s in stops_raw)
    elif isinstance(origin, dict) and isinstance(dest, dict):
        stops = (_end_stop(origin, depart=True, at=at), _end_stop(dest, depart=False, at=at))
    else:
        raise MalformedAnswer("leg has no stops and no origin/destination")
    planned_dep = stops[0].planned_departure
    if planned_dep is None:
        raise MalformedAnswer("leg has no planned departure at its boarding stop")
    prod = raw.get("product")
    prod = prod if isinstance(prod, dict) else {}
    tm = [
        _int_in(m.get("message"))
        for m in raw.get("transferMessages") or []
        if isinstance(m, dict) and m.get("type") == "TRANSFER_TIME"
    ]
    tt, cp = raw.get("transferTimeToNextLeg"), raw.get("crossPlatformTransfer")
    msgs = tuple(
        str(m.get("text") or m.get("message"))
        for m in raw.get("messages") or []
        if isinstance(m, dict) and (m.get("text") or m.get("message"))
    )
    return Leg(
        train=TrainId(planned_dep.date(), str(prod.get("number", "")), planned_dep),
        category=str(prod.get("categoryCode", "")),
        stops=stops,
        cancelled=bool(raw.get("cancelled", False)),
        part_cancelled=bool(raw.get("partCancelled", False)),
        change_possible=_opt_bool(raw.get("changePossible")),
        reachable=_opt_bool(raw.get("reachable")),
        alternative_transport=prod.get("type") != "TRAIN",
        messages=msgs,
        transfer_time_to_next=tt if isinstance(tt, int) and not isinstance(tt, bool) else None,
        transfer_message_min=next((m for m in tm if m is not None), None),
        cross_platform=_opt_bool(cp),
    )


def _opt_bool(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


def map_trips(payload: object, q: Query, fetched_at: datetime) -> ProviderAnswer:
    if not isinstance(payload, dict) or not isinstance(payload.get("trips"), list):
        raise MalformedAnswer("answer has no trips list")
    conns: list[Connection] = []
    for trip in payload["trips"]:
        legs_raw = trip.get("legs") if isinstance(trip, dict) else None
        if not isinstance(legs_raw, list) or not legs_raw:
            raise MalformedAnswer("trip has no legs")
        codes: dict[str, str] = {}
        for lr in legs_raw:
            for end in (lr.get("origin"), lr.get("destination")) if isinstance(lr, dict) else ():
                if isinstance(end, dict) and end.get("uicCode") and end.get("stationCode"):
                    codes[str(end["uicCode"])] = str(end["stationCode"])
        legs = tuple(_leg(lr, codes, fetched_at) for lr in legs_raw)
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


def map_disruptions(payload: object) -> PlannedWorks:
    """Planned works only. Any unreadable item makes coverage unknown (no probe may call itself clean)."""
    raw = _items(payload)
    if raw is None:
        return PlannedWorks((), "unknown")
    items: list[PlannedWork] = []
    complete = True
    for d in raw:
        if not isinstance(d, dict):
            complete = False
            continue
        if d.get("type") != "MAINTENANCE":
            continue
        start, end = _dt(d.get("start")), _dt(d.get("end"))
        stations: set[str] = set()
        for ps in d.get("publicationSections") or []:
            if not isinstance(ps, dict):
                continue
            for part in (ps.get("section"), (ps.get("consequence") or {}).get("section")):
                for s in (part or {}).get("stations") or []:
                    if isinstance(s, dict) and s.get("stationCode"):
                        stations.add(str(s["stationCode"]))
        if start is None or end is None:
            complete = False
            continue
        items.append(PlannedWork(start, end, frozenset(stations), str(d.get("title", ""))))
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

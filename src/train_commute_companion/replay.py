"""Replay files: a scripted world run through the real Runtime on a fake clock (9, build step 1).

A replay file is YAML: `profile` (path, relative to the file), `now` (local start, "YYYY-MM-DD
HH:MM"), `until` ("HH:MM" or "YYYY-MM-DD HH:MM"), `queries` (a list of `{at, query, answer_file}`)
and `events` (a list of `{at, action}` or `{at, location}`). An answer file lists trips made of
legs; it is turned into the provider's trips JSON and read by the real NS mapper."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from train_commute_companion.calendar import TZ, resolve_local
from train_commute_companion.lifecycle import Leaving, NotTravelling
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import Connection
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.providers.ns import map_trips
from train_commute_companion.runtime import Runtime
from train_commute_companion.store import PersistedState

ATTRIBUTION = "Data: NS"


class ReplayError(Exception):
    pass


def _local(text: object, base: date, what: str) -> datetime:
    """A quoted "HH:MM" (on the base date) or "YYYY-MM-DD HH:MM", as an Amsterdam instant by the
    runtime's own resolver (7.13): a time in the spring gap moves to the first instant after it,
    a repeated autumn time is its first occurrence. An explicit UTC offset ("02:30+01:00")
    names the second occurrence of a repeated time."""
    if not isinstance(text, str):
        raise ReplayError(f'{what}: quote the time, for example "08:12" (got {text!r})')
    m = re.fullmatch(r"(?:(\d{4}-\d{2}-\d{2}) )?(\d{1,2}):(\d{2})([+-]\d{2}:\d{2})?", text.strip())
    if not m:
        raise ReplayError(f"{what}: {text!r} is not HH:MM or YYYY-MM-DD HH:MM")
    day = date.fromisoformat(m.group(1)) if m.group(1) else base
    try:
        clock = time(int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ReplayError(f"{what}: {text!r} is not a time") from None
    if m.group(4) is None:
        return resolve_local(day, clock, edge="start")
    fixed = datetime.fromisoformat(f"{day.isoformat()}T{clock:%H:%M}{m.group(4)}").astimezone(TZ)
    if (fixed.date(), fixed.time()) != (day, clock):
        raise ReplayError(f"{what}: {text!r} is not an Amsterdam time at that offset")
    return fixed


class ReplayClock:
    def __init__(self, now: datetime) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t = (self.t.astimezone(UTC) + timedelta(seconds=seconds)).astimezone(TZ)


class MemoryStore:
    def __init__(self) -> None:
        self._s: PersistedState | None = None

    def load(self) -> PersistedState | None:
        return self._s

    def save(self, s: PersistedState) -> None:
        self._s = s


class PrintNotifier:
    async def send(self, event: Any) -> bool:
        return True


def _end(
    lg: dict[str, Any], code: str, hhmm: str, delay: int, day: date, name: str
) -> dict[str, Any]:
    planned = _local(hhmm, day, name)
    actual = (planned.astimezone(UTC) + timedelta(minutes=delay)).astimezone(TZ)
    gone = bool(lg.get("cancelled", False))  # a cancelled stop reports no time
    fmt = "%Y-%m-%dT%H:%M:%S%z"
    return {
        "name": code,
        "stationCode": code,
        "plannedDateTime": planned.strftime(fmt),
        **({} if gone else {"actualDateTime": actual.strftime(fmt)}),
        "plannedTrack": str(lg.get("track", "1")),
        "actualTrack": str(lg.get("new_track", lg.get("track", "1"))),
        "cancelled": gone,
    }


def _leg_json(lg: dict[str, Any], day: date, name: str) -> dict[str, Any]:
    delay = int(lg.get("delay", 0))
    return {
        "name": f"IC {lg['number']}",
        "cancelled": bool(lg.get("cancelled", False)),
        "partCancelled": bool(lg.get("cancelled", False)),
        "changePossible": True,
        "reachable": True,
        "origin": _end(lg, lg["from"], lg["dep"], delay, day, name),
        "destination": _end(lg, lg["to"], lg["arr"], int(lg.get("arr_delay", delay)), day, name),
        "product": {"number": str(lg["number"]), "categoryCode": "IC", "type": "TRAIN"},
    }


def _trips_json(raw: Any, base: date, name: str) -> dict[str, Any]:
    """The NS trips JSON for a compact answer file (`trips: [{legs: [{from, to, dep, arr, ...}]}]`).
    No transferTimeToNextLeg: NS states a minimum there, not the live gap."""
    if not isinstance(raw, dict) or not isinstance(raw.get("trips"), list):
        raise ReplayError(f"{name}: needs a 'trips' list")
    trips = []
    for t in raw["trips"]:
        day = base + timedelta(days=int(t.get("day", 0)))
        legs = [_leg_json(lg, day, name) for lg in t["legs"]]
        trips.append({"status": t.get("status", "NORMAL"), "ctxRecon": "replay", "legs": legs})
    return {"trips": trips}


def _pieces(c: Connection, origin: str, destination: str) -> list[Connection]:
    n = len(c.legs)
    return [
        Connection(c.legs[i:j], c.status, None)
        for i in range(n)
        for j in range(i + 1, n + 1)
        if c.legs[i].board.code == origin and c.legs[j - 1].alight.code == destination
    ]


@dataclass(frozen=True, slots=True)
class _Scripted:
    at: datetime
    query: str
    trips: dict[str, Any]


class ReplayProvider:
    """Answers each query from the newest scripted answer (per journey) that has started.
    A query for part of a scripted journey gets that part. Nothing scripted: a healthy,
    empty answer."""

    def __init__(self, clock: ReplayClock, script: list[_Scripted]) -> None:
        self._clock = clock
        self._script = sorted(script, key=lambda s: s.at.astimezone(UTC))

    async def trips(self, q: Query) -> ProviderAnswer:
        now = self._clock.now()
        live: dict[str, _Scripted] = {}
        for s in self._script:
            if s.at.astimezone(UTC) <= now.astimezone(UTC):  # instants: the DST fold
                live[s.query] = s
        conns = tuple(
            x
            for s in live.values()
            for c in map_trips(s.trips, q, now).connections
            for x in _pieces(c, q.origin, q.destination)
        )
        return ProviderAnswer(q, conns, now)

    async def planned_works(self) -> PlannedWorks:
        return PlannedWorks((), "complete")

    async def stations(self) -> frozenset[str]:
        return frozenset()


def format_event(e: Any) -> str:
    return f"{e.created_at.astimezone(TZ):%H:%M} {e.severity.name} {e.kind.value}: {e.text}"


def _read(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ReplayError(f"cannot read {path.name}: {e}") from None


async def replay_lines(path: Path) -> list[str]:
    spec = _read(path)
    if not isinstance(spec, dict):
        raise ReplayError(f"{path.name}: not a replay file")
    for key in ("profile", "now", "until"):
        if key not in spec:
            raise ReplayError(f"{path.name}: missing '{key}'")
    if not re.match(r"\d{4}-", str(spec["now"])):
        raise ReplayError("now: needs a date, YYYY-MM-DD HH:MM")
    start = _local(spec["now"], date.min, "now")
    base = start.date()
    end = _local(spec["until"], base, "until")
    script = []
    for i, q in enumerate(spec.get("queries") or []):
        ans = path.parent / str(q["answer_file"])
        script.append(
            _Scripted(
                _local(q["at"], base, f"queries[{i}].at"),
                str(q["query"]),
                _trips_json(_read(ans), base, ans.name),
            )
        )
    timeline = sorted(
        (
            (_local(e["at"], base, f"events[{i}].at"), e)
            for i, e in enumerate(spec.get("events") or [])
        ),
        key=lambda x: x[0].astimezone(UTC),
    )
    clock = ReplayClock(start)
    rt = Runtime(
        profile_path=path.parent / str(spec["profile"]),
        store=MemoryStore(),
        provider=ReplayProvider(clock, script),
        notifier=PrintNotifier(),
        clock=clock,
        known_stations=None,
    )
    await rt.start()
    pending = list(timeline)
    while clock.now().astimezone(UTC) < end.astimezone(UTC):
        while pending and pending[0][0].astimezone(UTC) <= clock.now().astimezone(UTC):
            await _apply(rt, pending.pop(0)[1], clock.now())
        now = clock.now()
        if now.minute % 2 == 0 and now.second == 0:
            await rt.poll_once()
        await rt.watchdog_once()
        await clock.sleep(30)
    return [*(format_event(e) for e in rt.events_log), ATTRIBUTION]


async def _apply(rt: Runtime, e: dict[str, Any], now: datetime) -> None:
    cfg = rt.config
    pid = str(e.get("profile") or (cfg.profiles[0].id if cfg and cfg.profiles else ""))
    if "location" in e:
        kind = str(e.get("kind", "enter"))
        if kind not in ("enter", "leave", "report"):
            raise ReplayError(f"location kind {kind!r}: use enter, leave or report")
        await rt.location(pid, LocationEvent(str(e["location"]), kind, now, now))  # type: ignore[arg-type]
    elif e.get("action") == "leaving":
        await rt.act(pid, Leaving(None, now))
    elif e.get("action") == "not_travelling":
        await rt.act(pid, NotTravelling(None, bool(e.get("whole_day", False))))
    else:
        raise ReplayError(f"unknown event {e!r}: use action leaving|not_travelling or location")

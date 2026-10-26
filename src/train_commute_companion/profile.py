"""Load, validate and overlap-check profiles.yaml. Reports every error, never stops at the first."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast

import yaml

from train_commute_companion.calendar import TZ, is_commute_day
from train_commute_companion.model import (
    AlertSettings,
    Config,
    Direction,
    LocationSettings,
    Profile,
    UsualJourney,
)
from train_commute_companion.schedule import dated_schedule

ALLOWED_TRAVELLER = "me"
FALLBACK_TRANSFER_MIN = 5
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_SOURCES = ("ha", "telegram", "owntracks")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# a year and two weeks from two days back: every weekday pair meets both DST nights
_SCAN_DAYS = 380
# room for the -3..+3 day scan and next-day returns without date overflow
_DATE_MIN, _DATE_MAX = date(2, 1, 1), date(9998, 12, 31)


class _Loader(yaml.SafeLoader):
    """Leaves YAML timestamps as text, so _dates is the one place that turns text into dates."""


def _as_text(loader: _Loader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(cast(yaml.ScalarNode, node)))


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _as_text)


@dataclass(frozen=True, slots=True)
class ProfileError:
    profile_id: str | None
    path: str
    message: str
    suggestion: str | None = None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")


class _Ctx:
    """Collects errors for one profile."""

    def __init__(self, pid: str | None, base: str) -> None:
        self.pid, self.base = pid, base
        self.errors: list[ProfileError] = []

    def err(self, path: str, message: str, suggestion: str | None = None) -> None:
        self.errors.append(ProfileError(self.pid, f"{self.base}.{path}", message, suggestion))

    def time(self, v: object, path: str) -> time | None:
        m = _TIME_RE.match(v) if isinstance(v, str) else None
        if m and int(m[1]) < 24 and int(m[2]) < 60:
            return time(int(m[1]), int(m[2]))
        hint = 'write times as quoted "HH:MM"' + (
            " (unquoted 08:12 is read as a number)" if isinstance(v, int) else ""
        )
        self.err(path, f"not a valid time: {v!r}", hint)
        return None

    def keys(self, d: Mapping[str, Any], allowed: Sequence[str], path: str) -> None:
        for k in d:
            if k not in allowed:
                self.err(
                    f"{path}.{k}".lstrip("."),
                    f"unknown field {k!r}",
                    f"allowed: {', '.join(allowed)}",
                )

    def integer(self, v: object, path: str, lo: int, hi: int | None = None) -> int | None:
        if isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi):
            return v
        self.err(path, f"must be a whole number of at least {lo}, got {v!r}")
        return None

    def boolean(self, v: object, path: str) -> bool | None:
        if isinstance(v, bool):
            return v
        self.err(path, f"must be true or false, got {v!r}")
        return None


def _shift(t: time, minutes: int) -> time:
    total = (t.hour * 60 + t.minute + minutes) % 1440
    return time(total // 60, total % 60)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _station(c: _Ctx, v: object, path: str, known: frozenset[str] | None) -> str | None:
    if not isinstance(v, str) or not v.strip():
        c.err(path, f"missing or not a station code: {v!r}")
        return None
    if known is not None and v not in known:
        close = v.strip().upper()
        c.err(
            path,
            f"unknown station code {v!r}",
            f"did you mean {close!r}?" if close in known else "use a code from the NS station list",
        )
        return None
    return v


def _journey(
    c: _Ctx, raw: object, name: str, known: frozenset[str] | None, *, allow_offset: bool
) -> UsualJourney | None:
    if not isinstance(raw, dict):
        c.err(name, "missing or not a mapping")
        return None
    n = len(c.errors)
    c.keys(
        raw,
        ("first_train", "transfers", "window", "buffer_min", "earlier_train_advice", "day_offset"),
        name,
    )
    if "day_offset" in raw and not allow_offset:
        c.err(
            f"{name}.day_offset", "day_offset is allowed on return only", "remove it from outbound"
        )
    first = c.time(raw.get("first_train"), f"{name}.first_train")
    tr = raw.get("transfers", [])
    transfers: list[str] = []
    if not isinstance(tr, list):
        c.err(f"{name}.transfers", "must be a list of station codes, [] for a direct train")
    else:
        for i, t in enumerate(tr):
            s = _station(c, t, f"{name}.transfers[{i}]", known)
            if s:
                transfers.append(s)
    buf = c.integer(raw.get("buffer_min"), f"{name}.buffer_min", 0)
    advice = c.boolean(
        raw.get("earlier_train_advice", name == "return"), f"{name}.earlier_train_advice"
    )
    offset = 0
    if allow_offset:
        offset = c.integer(raw.get("day_offset", 0), f"{name}.day_offset", 0, 1) or 0
    window: tuple[time, time] | None = None
    w = raw.get("window")
    if w is None and first is not None:
        window = (_shift(first, -60), _shift(first, 120))
    elif isinstance(w, list) and len(w) == 2:
        a, b = c.time(w[0], f"{name}.window[0]"), c.time(w[1], f"{name}.window[1]")
        window = (a, b) if a and b else None
    elif w is not None:
        c.err(f"{name}.window", 'must be two times, for example ["07:12", "10:12"]')
    if first and window and buf is not None:
        before = (_minutes(first) - _minutes(window[0])) % 1440
        after = (_minutes(window[1]) - _minutes(first)) % 1440
        if before + after != (_minutes(window[1]) - _minutes(window[0])) % 1440:
            c.err(f"{name}.window", "window does not contain first_train")
        elif after == 0:  # windows are half-open, so the end itself is outside
            c.err(
                f"{name}.window",
                "window closes at first_train, so the train itself is outside it",
                "close the window after first_train",
            )
        elif before < buf + 15:
            c.err(
                f"{name}.window",
                f"window opens {before} min before first_train, needs at least {buf + 15} (buffer_min + 15)",
                f"open the window at {_shift(first, -(buf + 15)):%H:%M} or earlier",
            )
    if len(c.errors) > n or first is None or window is None or buf is None or advice is None:
        return None
    return UsualJourney(first, tuple(transfers), window, buf, advice, offset)


def _alerts(c: _Ctx, raw: object) -> AlertSettings | None:
    if raw is None:
        return AlertSettings()
    if not isinstance(raw, dict):
        c.err("alerts", "must be a mapping")
        return None
    n = len(c.errors)
    c.keys(
        raw,
        (
            "earlier_train_threshold_min",
            "earlier_train_min_saving_min",
            "latest_acceptable_arrival",
            "quiet_hours",
        ),
        "alerts",
    )
    d = AlertSettings()
    thr = c.integer(
        raw.get("earlier_train_threshold_min", d.earlier_train_threshold_min),
        "alerts.earlier_train_threshold_min",
        0,
    )
    sav = c.integer(
        raw.get("earlier_train_min_saving_min", d.earlier_train_min_saving_min),
        "alerts.earlier_train_min_saving_min",
        0,
    )
    lat = raw.get("latest_acceptable_arrival")
    late = c.time(lat, "alerts.latest_acceptable_arrival") if lat is not None else None
    q = raw.get("quiet_hours")
    quiet: tuple[time, time] | None = None
    if q is not None:
        if isinstance(q, list) and len(q) == 2:
            a, b = c.time(q[0], "alerts.quiet_hours[0]"), c.time(q[1], "alerts.quiet_hours[1]")
            quiet = (a, b) if a and b else None
        else:
            c.err("alerts.quiet_hours", 'must be two times or null, for example ["19:52", "07:12"]')
    if len(c.errors) > n or thr is None or sav is None:
        return None
    return AlertSettings(thr, sav, late, quiet)


def _location(c: _Ctx, raw: object) -> tuple[bool, LocationSettings | None]:
    if raw is None:
        return True, None
    if not isinstance(raw, dict):
        c.err("location", "must be a mapping or null")
        return False, None
    n = len(c.errors)
    c.keys(raw, ("source", "entity", "zones"), "location")
    src, ent, zones = raw.get("source"), raw.get("entity"), raw.get("zones", {})
    if src not in _SOURCES:
        c.err("location.source", f"must be one of {', '.join(_SOURCES)}, got {src!r}")
    if ent is not None and not isinstance(ent, str):
        c.err("location.entity", "must be text")
    if src == "ha" and not ent:
        c.err("location.entity", "source ha needs an entity", "for example person.someone")
    if not isinstance(zones, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in zones.items()
    ):
        c.err("location.zones", "must map names to zone names")
    if len(c.errors) > n:
        return False, None
    return True, LocationSettings(
        cast(Literal["ha", "telegram", "owntracks"], src), ent, dict(zones)
    )


def _dates(c: _Ctx, raw: object) -> frozenset[date] | None:
    if not isinstance(raw, list):
        c.err("extra_dates", "must be a list of YYYY-MM-DD dates, [] for none")
        return None
    out: set[date] = set()
    ok = True
    for i, v in enumerate(raw):
        try:
            if not (isinstance(v, str) and _DATE_RE.match(v)):
                raise ValueError(v)
            d = date.fromisoformat(v)
        except ValueError:
            c.err(f"extra_dates[{i}]", f"not a YYYY-MM-DD date: {v!r}")
            ok = False
            continue
        if _DATE_MIN <= d <= _DATE_MAX:
            out.add(d)
        else:
            c.err(f"extra_dates[{i}]", f"{v} is outside {_DATE_MIN} .. {_DATE_MAX}")
            ok = False
    return frozenset(out) if ok else None


def _profile(
    raw: object, idx: int, known: frozenset[str] | None
) -> tuple[Profile | None, list[ProfileError]]:
    base = f"profiles[{idx}]"
    if not isinstance(raw, dict):
        return None, [ProfileError(None, base, "each profile must be a mapping")]
    pid = raw.get("id")
    c = _Ctx(pid if isinstance(pid, str) and pid else None, base)
    c.keys(
        raw,
        (
            "id",
            "name",
            "traveller",
            "days",
            "extra_dates",
            "skip_public_holidays",
            "home_station",
            "work_station",
            "outbound",
            "return",
            "transfer_min",
            "alerts",
            "location",
        ),
        "",
    )
    name = raw.get("name")
    if not isinstance(name, str) or not slug(name):
        c.err("name", f"missing or empty name: {name!r}")
        name = None
    if c.pid is None:
        c.err(
            "id",
            "missing id",
            f'add id: "{slug(name) if name else "p1"}" (any unique text, never changed later)',
        )
    trav = raw.get("traveller", ALLOWED_TRAVELLER)
    if trav != ALLOWED_TRAVELLER:
        c.err("traveller", f"only {ALLOWED_TRAVELLER!r} is allowed in this version, got {trav!r}")
    days: set[int] = set()
    rd = raw.get("days", [])
    if not isinstance(rd, list):
        c.err("days", "must be a list such as [tue, wed]")
    else:
        for i, d in enumerate(rd):
            if isinstance(d, str) and d.lower() in _DAYS:
                days.add(_DAYS.index(d.lower()) + 1)
            else:
                c.err(f"days[{i}]", f"not a weekday: {d!r}", f"use {', '.join(_DAYS)}")
    extra = _dates(c, raw.get("extra_dates", []))
    if not days and extra == frozenset() and isinstance(rd, list):
        c.err("days", "no days and no extra_dates: the profile would never run")
    skip = c.boolean(raw.get("skip_public_holidays", True), "skip_public_holidays")
    home = _station(c, raw.get("home_station"), "home_station", known)
    work = _station(c, raw.get("work_station"), "work_station", known)
    out = _journey(c, raw.get("outbound"), "outbound", known, allow_offset=False)
    ret = _journey(c, raw.get("return"), "return", known, allow_offset=True)
    tm_raw = raw.get("transfer_min", {})
    tm: dict[str, int] = {}
    if not isinstance(tm_raw, dict):
        c.err("transfer_min", "must be a mapping such as { UT: 4 }")
    else:
        for k, v in tm_raw.items():
            iv = c.integer(v, f"transfer_min.{k}", 1)
            if iv is not None:
                tm[str(k)] = iv
        for key in ("outbound", "return"):
            j = raw.get(key)
            tl = j.get("transfers") if isinstance(j, dict) else None
            for s in tl if isinstance(tl, list) else ():
                if isinstance(s, str) and s not in tm_raw:
                    c.err(
                        f"transfer_min.{s}",
                        f"no transfer_min for transfer station {s}",
                        f"add transfer_min: {{ {s}: {FALLBACK_TRANSFER_MIN} }} "
                        f"({FALLBACK_TRANSFER_MIN} min is the fallback)",
                    )
    alerts = _alerts(c, raw.get("alerts"))
    loc_ok, loc = _location(c, raw.get("location"))
    if c.errors or not (
        out
        and ret
        and home
        and work
        and alerts
        and loc_ok
        and extra is not None
        and skip is not None
        and name
        and c.pid
    ):
        return None, c.errors
    return Profile(
        c.pid,
        name,
        ALLOWED_TRAVELLER,
        frozenset(days),
        extra,
        skip,
        home,
        work,
        out,
        ret,
        tm,
        alerts,
        loc,
    ), []


def _load_yaml(text: str) -> tuple[Any, list[ProfileError]]:
    try:
        return yaml.load(text.lstrip("﻿"), Loader=_Loader), []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark else ""
        problem = getattr(e, "problem", None) or "invalid YAML"
        tab = (
            " (tabs are not allowed, indent with spaces)"
            if "\t" in problem or "tab" in problem
            else ""
        )
        return None, [ProfileError(None, "file", f"{where}{problem}{tab}", "fix the YAML syntax")]
    except ValueError as e:  # an explicit tag such as !!int on text that is not one
        return None, [ProfileError(None, "file", f"invalid value: {e}", "fix the YAML syntax")]


def parse_profiles(
    text: str, known_stations: frozenset[str] | None
) -> tuple[list[Profile], list[ProfileError]]:
    data, errors = _load_yaml(text)
    if errors:
        return [], errors
    items = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return [], [
            ProfileError(
                None,
                "profiles",
                "file needs a non-empty 'profiles' list",
                "start from examples/profiles.yaml",
            )
        ]
    profiles: list[Profile] = []
    for i, raw in enumerate(items):
        p, errs = _profile(raw, i, known_stations)
        errors += errs
        if p:
            profiles.append(p)
    seen_id: dict[str, int] = {}
    seen_name: dict[str, int] = {}
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        pid, nm = raw.get("id"), raw.get("name")
        if isinstance(pid, str) and pid in seen_id:
            errors.append(
                ProfileError(
                    pid, f"profiles[{i}].id", f"duplicate id {pid!r}", "ids must be unique"
                )
            )
        elif isinstance(pid, str):
            seen_id[pid] = i
        key = slug(nm) if isinstance(nm, str) else ""
        if key and key in seen_name:
            errors.append(
                ProfileError(
                    pid if isinstance(pid, str) else None,
                    f"profiles[{i}].name",
                    f"duplicate name {nm!r}",
                    "names must be unique (they become entity prefixes)",
                )
            )
        elif key:
            seen_name[key] = i
    dup = {e.profile_id for e in errors if e.message.startswith("duplicate")}
    # a duplicate poisons every profile that shares the value
    # counted over raw entries, so a valid profile sharing a name with an invalid one is refused too
    names = [
        slug(r["name"]) for r in items if isinstance(r, dict) and isinstance(r.get("name"), str)
    ]
    dup_names = {k for k in names if k and names.count(k) > 1}
    profiles = [p for p in profiles if p.id not in dup and slug(p.name) not in dup_names]
    return profiles, errors


FirstTrain = Callable[[Profile, Direction, date], time | None]


@dataclass(frozen=True, slots=True)
class _Occ:
    day: date
    direction: Direction
    recurring: bool  # on one of the profile's weekdays, holidays ignored
    real: bool  # an actual commute day
    start: datetime  # UTC
    end: datetime  # UTC


def _occurrences(
    p: Profile, today: date, near: frozenset[date], first_train: FirstTrain | None
) -> dict[date, list[_Occ]]:
    first = today - timedelta(days=2)  # a commute day two days back can still have an open window
    span = {first + timedelta(days=i) for i in range(_SCAN_DAYS)}
    out: dict[date, list[_Occ]] = {}
    for day in sorted(span | {d for d in near if d >= first}):
        recurring = day.isoweekday() in p.days
        real = is_commute_day(day, p.days, p.extra_dates, p.skip_public_holidays)
        if recurring or real:
            for d in Direction:
                s = dated_schedule(p, d, day, first_train(p, d, day) if first_train else None)
                out.setdefault(day, []).append(
                    _Occ(
                        day,
                        d,
                        recurring,
                        real,
                        s.window_start.astimezone(UTC),
                        s.window_end.astimezone(UTC),
                    )
                )
    return out


def _window_overlap(
    oa: dict[date, list[_Occ]], ob: dict[date, list[_Occ]], floor: datetime, same: bool
) -> date | None:
    """Earliest local date two windows overlap, half-open [start, end) like in_window."""
    hits: list[datetime] = []
    for day, xs in oa.items():
        for k in range(-3, 4):  # a dated window lies within its commute day -1 .. +2
            for y in ob.get(day + timedelta(days=k), ()):
                for x in xs:
                    if same and (x.day, x.direction) == (y.day, y.direction):
                        continue  # the same occurrence when a profile is checked against itself
                    # a weekly pattern collides in every non-holiday week, so holidays only
                    # count when a one-off date is involved
                    if not ((x.real and y.real) or (x.recurring and y.recurring)):
                        continue
                    lo, hi = max(x.start, y.start), min(x.end, y.end)
                    if lo < hi and hi > floor:
                        hits.append(lo)
    return min(hits).astimezone(TZ).date() if hits else None


def _overlap_pairs(
    profiles: Sequence[Profile], today: date, first: FirstTrain | None = None
) -> list[tuple[Profile, Profile, ProfileError]]:
    found: dict[tuple[str, str], tuple[Profile, Profile, ProfileError]] = {}

    def add(a: Profile, b: Profile, msg: str, hint: str) -> None:
        found.setdefault((a.id, b.id), (a, b, ProfileError(a.id, "profiles", msg, hint)))

    pairs = [(a, b) for i, a in enumerate(profiles) for b in profiles[i + 1 :]]
    # a shared weekday collides on every non-holiday week
    for a, b in pairs:
        shared = a.days & b.days
        if shared:
            msg = f"profiles {a.name!r} and {b.name!r} are both active on {_DAYS[min(shared) - 1]}"
            add(a, b, msg, "give them different days")
    # a one-off date collides with any other profile active that day, however far ahead
    for a, b in pairs:
        hit = min(
            (
                d
                for x, y in ((a, b), (b, a))
                for d in x.extra_dates
                if d >= today and is_commute_day(d, y.days, y.extra_dates, y.skip_public_holidays)
            ),
            default=None,
        )
        if hit:
            msg = (
                f"profiles {a.name!r} and {b.name!r} are both active on "
                f"{_DAYS[hit.weekday()]} {hit.isoformat()}"
            )
            add(a, b, msg, "give them different days or remove the extra date")
    # different commute days, or two directions of one profile, can still collide in real time
    near = frozenset(
        d + timedelta(days=k) for p in profiles for d in p.extra_dates for k in range(-3, 4)
    )
    occ = {p.id: _occurrences(p, today, near, first) for p in profiles}
    floor = datetime.combine(today, time(), TZ).astimezone(UTC)
    for p in profiles:
        hit = _window_overlap(occ[p.id], occ[p.id], floor, True)
        if hit:
            msg = f"profile {p.name!r} has overlapping time windows on {hit.isoformat()}"
            add(p, p, msg, "narrow the outbound or return window")
    for a, b in pairs:
        if (a.id, b.id) not in found:
            hit = _window_overlap(occ[a.id], occ[b.id], floor, False)
            if hit:
                msg = (
                    f"profiles {a.name!r} and {b.name!r} have overlapping time windows "
                    f"on {hit.isoformat()}"
                )
                add(a, b, msg, "narrow a window or change the days")
    return list(found.values())


def find_overlaps(
    profiles: Sequence[Profile], today: date, first: FirstTrain | None = None
) -> list[ProfileError]:
    """`first` gives the effective first train of a (profile, direction, commute day), for the
    dated check after an adoption (7.12 rule 19); without it the file's first trains count."""
    return [e for _, _, e in _overlap_pairs(profiles, today, first)]


def load_config(
    text: str, known_stations: frozenset[str] | None, previous: Config | None, today: date
) -> tuple[Config | None, list[ProfileError]]:
    profiles, errors = parse_profiles(text, known_stations)
    pairs = _overlap_pairs(profiles, today)
    errors = errors + [e for _, _, e in pairs]
    if previous is not None:
        if errors:
            return None, errors
        if tuple(profiles) == previous.profiles:
            return previous, []
        return Config(previous.revision + 1, tuple(profiles)), []
    refused = {p.id for a, b, _ in pairs for p in (a, b)}
    kept = tuple(p for p in profiles if p.id not in refused)
    return (Config(1, kept) if kept else None), errors

"""Load, validate and overlap-check profiles.yaml. Reports every error, never stops at the first."""

import re
from collections.abc import Mapping, Sequence
from datetime import date, time, timedelta
from typing import Any, cast, get_args

import yaml

from train_commute_companion.calendar import MINUTES_PER_DAY, WEEKDAYS, minute_of_day
from train_commute_companion.model import (
    AccessibilitySettings,
    AlertSettings,
    Config,
    Direction,
    LocationSettings,
    LocationSource,
    NotificationCategory,
    NotificationChannel,
    Profile,
    ProfileProblem,
    UsualJourney,
    WorksDigestSettings,
)
from train_commute_companion.notification_policy import validate_channels
from train_commute_companion.overlap import overlap_pairs
from train_commute_companion.transfer import FALLBACK_MIN

ALLOWED_TRAVELLER = "me"
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_SOURCES: tuple[LocationSource, ...] = get_args(LocationSource)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# room for the -3..+3 day scan and next-day returns without date overflow
_DATE_MIN, _DATE_MAX = date(2, 1, 1), date(9998, 12, 31)


class _Loader(yaml.SafeLoader):
    """Leaves YAML timestamps as text, so _date validates every configured date."""


def _as_text(loader: _Loader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(cast(yaml.ScalarNode, node)))


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _as_text)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")


class _ProfileErrors:
    """Collects errors for one profile."""

    def __init__(self, pid: str | None, base: str) -> None:
        self.pid, self.base = pid, base
        self.errors: list[ProfileProblem] = []

    def err(self, path: str, message: str, suggestion: str | None = None) -> None:
        self.errors.append(ProfileProblem(self.pid, f"{self.base}.{path}", message, suggestion))

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
    total = (t.hour * 60 + t.minute + minutes) % MINUTES_PER_DAY
    return time(total // 60, total % 60)


def _station(c: _ProfileErrors, v: object, path: str, known: frozenset[str] | None) -> str | None:
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


def _transfers(
    c: _ProfileErrors, raw: object, name: str, known: frozenset[str] | None
) -> list[str]:
    if not isinstance(raw, list):
        c.err(f"{name}.transfers", "must be a list of station codes, [] for a direct train")
        return []
    found = (_station(c, t, f"{name}.transfers[{i}]", known) for i, t in enumerate(raw))
    return [s for s in found if s]


def _window(
    c: _ProfileErrors, raw: object, name: str, first: time | None
) -> tuple[time, time] | None:
    if raw is None and first is not None:
        return _shift(first, -60), _shift(first, 120)
    if isinstance(raw, list) and len(raw) == 2:
        a, b = c.time(raw[0], f"{name}.window[0]"), c.time(raw[1], f"{name}.window[1]")
        return (a, b) if a and b else None
    if raw is not None:
        c.err(f"{name}.window", 'must be two times, for example ["07:12", "10:12"]')
    return None


def _check_window(
    c: _ProfileErrors, name: str, first: time, window: tuple[time, time], buf: int
) -> None:
    before = (minute_of_day(first) - minute_of_day(window[0])) % MINUTES_PER_DAY
    after = (minute_of_day(window[1]) - minute_of_day(first)) % MINUTES_PER_DAY
    span = (minute_of_day(window[1]) - minute_of_day(window[0])) % MINUTES_PER_DAY
    if before + after != span:
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


def _journey(
    c: _ProfileErrors, raw: object, name: str, known: frozenset[str] | None, *, allow_offset: bool
) -> UsualJourney | None:
    if not isinstance(raw, dict):
        c.err(name, "missing or not a mapping")
        return None
    n = len(c.errors)
    c.keys(
        raw,
        (
            "first_train",
            "first_train_by_weekday",
            "transfers",
            "window",
            "buffer_min",
            "earlier_train_advice",
            "day_offset",
        ),
        name,
    )
    if "day_offset" in raw and not allow_offset:
        c.err(
            f"{name}.day_offset", "day_offset is allowed on return only", "remove it from outbound"
        )
    first = c.time(raw.get("first_train"), f"{name}.first_train")
    transfers = _transfers(c, raw.get("transfers", []), name, known)
    buf = c.integer(raw.get("buffer_min"), f"{name}.buffer_min", 0)
    advice = c.boolean(
        raw.get("earlier_train_advice", name == "return"), f"{name}.earlier_train_advice"
    )
    offset = 0
    if allow_offset:
        offset = c.integer(raw.get("day_offset", 0), f"{name}.day_offset", 0, 1) or 0
    window = _window(c, raw.get("window"), name, first)
    if first and window and buf is not None:
        _check_window(c, name, first, window, buf)
    raw_weekdays = raw.get("first_train_by_weekday", {})
    weekday_times: dict[str, time] = {}
    if not isinstance(raw_weekdays, dict):
        c.err(f"{name}.first_train_by_weekday", "must map mon through sun to quoted HH:MM times")
    else:
        c.keys(raw_weekdays, WEEKDAYS, f"{name}.first_train_by_weekday")
        for weekday, value in raw_weekdays.items():
            parsed = c.time(value, f"{name}.first_train_by_weekday.{weekday}")
            if parsed is not None and weekday in WEEKDAYS:
                weekday_times[weekday] = parsed
    if len(c.errors) > n or first is None or window is None or buf is None or advice is None:
        return None
    return UsualJourney(first, tuple(transfers), window, buf, advice, offset, weekday_times)


def _alerts(c: _ProfileErrors, raw: object) -> AlertSettings | None:
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
            "disabled_categories",
            "channels",
            "compensation_reminder",
        ),
        "alerts",
    )
    d = AlertSettings()
    compensation = raw.get("compensation_reminder", False)
    if not isinstance(compensation, bool):
        c.err("alerts.compensation_reminder", "must be true or false")
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
    disabled = raw.get("disabled_categories", [])
    categories = get_args(NotificationCategory)
    if not isinstance(disabled, list) or any(
        not isinstance(value, str) or value not in categories for value in disabled
    ):
        c.err(
            "alerts.disabled_categories", f"must be a list containing only {', '.join(categories)}"
        )
        disabled = []
    raw_channels = raw.get("channels", {})
    channels: dict[NotificationCategory, frozenset[NotificationChannel]] = {}
    if not isinstance(raw_channels, dict):
        c.err("alerts.channels", "must map alert categories to non-empty channel lists")
    else:
        for category, selected in raw_channels.items():
            path = f"alerts.channels.{category}"
            if category not in categories:
                c.err(path, "unknown alert category")
            elif (
                not isinstance(selected, list)
                or not selected
                or any(
                    not isinstance(value, str) or value not in get_args(NotificationChannel)
                    for value in selected
                )
            ):
                c.err(path, "must be a non-empty list containing only primary, email, webhook")
            else:
                channels[cast(NotificationCategory, category)] = frozenset(
                    cast(list[NotificationChannel], selected)
                )
    if len(c.errors) > n or thr is None or sav is None:
        return None
    return AlertSettings(
        thr,
        sav,
        late,
        quiet,
        frozenset(cast(list[NotificationCategory], disabled)),
        channels,
        compensation,
    )


def _location(c: _ProfileErrors, raw: object) -> tuple[bool, LocationSettings | None]:
    if raw is None:
        return True, None
    if not isinstance(raw, dict):
        c.err("location", "must be a mapping or null")
        return False, None
    n = len(c.errors)
    c.keys(raw, ("source", "entity", "zones", "station_radius_m"), "location")
    src, ent, zones = raw.get("source"), raw.get("entity"), raw.get("zones", {})
    radius = raw.get("station_radius_m", 150)
    if isinstance(radius, bool) or not isinstance(radius, int) or radius <= 0:
        c.err("location.station_radius_m", "must be a whole number of metres above 0")
    source = next((s for s in _SOURCES if s == src), None)
    if source is None:
        c.err("location.source", f"must be one of {', '.join(_SOURCES)}, got {src!r}")
    if ent is not None and not isinstance(ent, str):
        c.err("location.entity", "must be text")
    if src == "ha" and not ent:
        c.err("location.entity", "source ha needs an entity", "for example person.someone")
    if not isinstance(zones, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in zones.items()
    ):
        c.err("location.zones", "must map names to zone names")
    if source is None or len(c.errors) > n:
        return False, None
    return True, LocationSettings(source, ent, dict(zones), radius)


def _date(c: _ProfileErrors, value: object, path: str) -> date | None:
    try:
        if not (isinstance(value, str) and _DATE_RE.match(value)):
            raise ValueError(value)
        day = date.fromisoformat(value)
    except ValueError:
        c.err(path, f"not a YYYY-MM-DD date: {value!r}")
        return None
    if not _DATE_MIN <= day <= _DATE_MAX:
        c.err(path, f"{value} is outside {_DATE_MIN} .. {_DATE_MAX}")
        return None
    return day


def _dates(c: _ProfileErrors, raw: object) -> frozenset[date] | None:
    if not isinstance(raw, list):
        c.err("extra_dates", "must be a list of YYYY-MM-DD dates, [] for none")
        return None
    out: set[date] = set()
    before = len(c.errors)
    for i, v in enumerate(raw):
        if (d := _date(c, v, f"extra_dates[{i}]")) is not None:
            out.add(d)
    return frozenset(out) if len(c.errors) == before else None


def _pauses(c: _ProfileErrors, raw: object) -> tuple[tuple[date, date], ...]:
    if not isinstance(raw, list):
        c.err("pauses", "must be a list of start/end date ranges, [] for none")
        return ()
    out: list[tuple[date, date]] = []
    for i, value in enumerate(raw):
        path = f"pauses[{i}]"
        if not isinstance(value, dict):
            c.err(path, "must be a mapping with start and end dates")
            continue
        c.keys(value, ("start", "end"), path)
        start = _date(c, value.get("start"), f"{path}.start")
        end = _date(c, value.get("end"), f"{path}.end")
        if start is not None and end is not None:
            if end < start:
                c.err(f"{path}.end", "must be on or after start")
            else:
                out.append((start, end))
    return tuple(out)


def _days(c: _ProfileErrors, rd: object) -> set[int]:
    days: set[int] = set()
    if not isinstance(rd, list):
        c.err("days", "must be a list such as [tue, wed]")
        return days
    for i, d in enumerate(rd):
        if isinstance(d, str) and d.lower() in WEEKDAYS:
            days.add(WEEKDAYS.index(d.lower()) + 1)
        else:
            c.err(f"days[{i}]", f"not a weekday: {d!r}", f"use {', '.join(WEEKDAYS)}")
    return days


def _transfer_min(c: _ProfileErrors, raw: Mapping[str, object]) -> dict[str, int]:
    """Reads the raw journeys, so a transfer station of a journey that failed to parse is still
    checked and every error comes out in one pass."""
    tm_raw = raw.get("transfer_min", {})
    tm: dict[str, int] = {}
    if not isinstance(tm_raw, dict):
        c.err("transfer_min", "must be a mapping such as { UT: 4 }")
        return tm
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
                    f"add transfer_min: {{ {s}: {FALLBACK_MIN} }} "
                    f"({FALLBACK_MIN} min is the fallback)",
                )
    return tm


_PROFILE_KEYS = (
    "id",
    "name",
    "traveller",
    "days",
    "extra_dates",
    "pauses",
    "skip_public_holidays",
    "home_station",
    "work_station",
    "outbound",
    "return",
    "transfer_min",
    "alerts",
    "location",
    "accessibility",
    "works_digest",
)


def _accessibility(c: _ProfileErrors, raw: object) -> AccessibilitySettings | None:
    if not isinstance(raw, dict):
        c.err("accessibility", "must be a mapping")
        return None
    c.keys(raw, ("require_accessible",), "accessibility")
    required = c.boolean(raw.get("require_accessible", False), "accessibility.require_accessible")
    return None if required is None else AccessibilitySettings(required)


def _works_digest(c: _ProfileErrors, raw: object) -> WorksDigestSettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        c.err("works_digest", "must be null or a mapping with day and time")
        return None
    c.keys(raw, ("day", "time"), "works_digest")
    day = raw.get("day")
    at = c.time(raw.get("time"), "works_digest.time")
    if not isinstance(day, str) or day not in WEEKDAYS:
        c.err("works_digest.day", "must be one of " + ", ".join(WEEKDAYS))
        return None
    return None if at is None else WorksDigestSettings(WEEKDAYS.index(day) + 1, at)


def _name(c: _ProfileErrors, raw: object) -> str | None:
    if isinstance(raw, str) and slug(raw):
        return raw
    c.err("name", f"missing or empty name: {raw!r}")
    return None


def _profile(
    raw: object, idx: int, known: frozenset[str] | None
) -> tuple[Profile | None, list[ProfileProblem]]:
    base = f"profiles[{idx}]"
    if not isinstance(raw, dict):
        return None, [ProfileProblem(None, base, "each profile must be a mapping")]
    pid = raw.get("id")
    c = _ProfileErrors(pid if isinstance(pid, str) and pid else None, base)
    c.keys(raw, _PROFILE_KEYS, "")
    name = _name(c, raw.get("name"))
    if c.pid is None:
        c.err(
            "id",
            "missing id",
            f'add id: "{slug(name) if name else "p1"}" (any unique text, never changed later)',
        )
    trav = raw.get("traveller", ALLOWED_TRAVELLER)
    if trav != ALLOWED_TRAVELLER:
        c.err("traveller", f"only {ALLOWED_TRAVELLER!r} is allowed in this version, got {trav!r}")
    rd = raw.get("days", [])
    days = _days(c, rd)
    extra = _dates(c, raw.get("extra_dates", []))
    pauses = _pauses(c, raw.get("pauses", []))
    if not days and extra == frozenset() and isinstance(rd, list):
        c.err("days", "no days and no extra_dates: the profile would never run")
    skip = c.boolean(raw.get("skip_public_holidays", True), "skip_public_holidays")
    home = _station(c, raw.get("home_station"), "home_station", known)
    work = _station(c, raw.get("work_station"), "work_station", known)
    out = _journey(c, raw.get("outbound"), "outbound", known, allow_offset=False)
    ret = _journey(c, raw.get("return"), "return", known, allow_offset=True)
    tm = _transfer_min(c, raw)
    alerts = _alerts(c, raw.get("alerts"))
    loc_ok, loc = _location(c, raw.get("location"))
    accessibility = _accessibility(c, raw.get("accessibility", {}))
    digest = _works_digest(c, raw.get("works_digest"))
    if c.errors or not (
        out
        and ret
        and home
        and work
        and alerts
        and loc_ok
        and accessibility is not None
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
        pauses,
        accessibility,
        digest,
    ), []


def _load_yaml(text: str) -> tuple[Any, list[ProfileProblem]]:
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
        return None, [ProfileProblem(None, "file", f"{where}{problem}{tab}", "fix the YAML syntax")]
    except ValueError as e:  # an explicit tag such as !!int on text that is not one
        return None, [ProfileProblem(None, "file", f"invalid value: {e}", "fix the YAML syntax")]
    except RecursionError:
        return None, [ProfileProblem(None, "file", "nested too deeply", "fix the YAML syntax")]


def _duplicates(items: Sequence[object]) -> list[ProfileProblem]:
    problems: list[ProfileProblem] = []
    seen_id: set[str] = set()
    seen_name: set[str] = set()
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        pid, nm = raw.get("id"), raw.get("name")
        if isinstance(pid, str) and pid in seen_id:
            problems.append(
                ProfileProblem(
                    pid, f"profiles[{i}].id", f"duplicate id {pid!r}", "ids must be unique"
                )
            )
        elif isinstance(pid, str):
            seen_id.add(pid)
        key = slug(nm) if isinstance(nm, str) else ""
        if key and key in seen_name:
            problems.append(
                ProfileProblem(
                    pid if isinstance(pid, str) else None,
                    f"profiles[{i}].name",
                    f"duplicate name {nm!r}",
                    "names must be unique (they become entity prefixes)",
                )
            )
        elif key:
            seen_name.add(key)
    return problems


def parse_profiles(
    text: str,
    known_stations: frozenset[str] | None,
    *,
    available_channels: frozenset[NotificationChannel] | None = None,
) -> tuple[list[Profile], list[ProfileProblem]]:
    data, errors = _load_yaml(text)
    if errors:
        return [], errors
    items = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return [], [
            ProfileProblem(
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
        if p is not None and available_channels is not None:
            unavailable = validate_channels((p,), available_channels)
            errors += [
                ProfileProblem(
                    e.profile_id,
                    e.path.replace("profiles[0]", f"profiles[{i}]", 1),
                    e.message,
                    e.suggestion,
                )
                for e in unavailable
            ]
            if unavailable:
                p = None
        if p:
            profiles.append(p)
    errors += _duplicates(items)
    dup = {e.profile_id for e in errors if e.message.startswith("duplicate")}
    # a duplicate poisons every profile that shares the value
    # counted over raw entries, so a valid profile sharing a name with an invalid one is refused too
    names = [
        slug(r["name"]) for r in items if isinstance(r, dict) and isinstance(r.get("name"), str)
    ]
    dup_names = {k for k in names if k and names.count(k) > 1}
    profiles = [p for p in profiles if p.id not in dup and slug(p.name) not in dup_names]
    return profiles, errors


def load_config(
    text: str,
    known_stations: frozenset[str] | None,
    previous: Config | None,
    today: date,
    *,
    available_channels: frozenset[NotificationChannel] | None = None,
) -> tuple[Config | None, list[ProfileProblem]]:
    profiles, errors = parse_profiles(text, known_stations, available_channels=available_channels)
    pairs = overlap_pairs(profiles, today)
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


_DST_HOUR = 2  # the hour that is repeated or skipped on the clock-change nights (5.2)


def _times(p: Profile) -> list[tuple[str, time]]:
    out: list[tuple[str, time]] = []
    for d in Direction:
        j = p.journey(d)
        out.append((f"{d.value}.first_train", j.first_train))
        out += [(f"{d.value}.window[{i}]", t) for i, t in enumerate(j.window)]
        for weekday, first in j.first_train_by_weekday.items():
            out.append((f"{d.value}.first_train_by_weekday.{weekday}", first))
            dated = p.journey_on(d, date(2026, 10, 26) + timedelta(days=WEEKDAYS.index(weekday)))
            out += [
                (f"{d.value}.first_train_by_weekday.{weekday}.window[{i}]", t)
                for i, t in enumerate(dated.window)
            ]
    if p.alerts.latest_acceptable_arrival:
        out.append(("alerts.latest_acceptable_arrival", p.alerts.latest_acceptable_arrival))
    out += [(f"alerts.quiet_hours[{i}]", t) for i, t in enumerate(p.alerts.quiet_hours or ())]
    return out


def check_lines(
    text: str, known: frozenset[str] | None, today: date
) -> tuple[Config | None, list[str]]:
    """`load_config` as readable lines: every error, then the clock-change hour warnings (5.3)."""
    cfg, errors = load_config(text, known, None, today)
    lines = [
        f"{e.path}: {e.message}" + (f" ({e.suggestion})" if e.suggestion else "") for e in errors
    ]
    for p in cfg.profiles if cfg else ():
        for where, t in _times(p):
            if t.hour == _DST_HOUR:
                lines.append(
                    f"warning: {p.name}.{where}: {t:%H:%M} is between {_DST_HOUR:02d}:00 and "
                    f"{_DST_HOUR + 1:02d}:00, which is repeated or skipped on the clock-change nights"
                )
    return cfg, lines

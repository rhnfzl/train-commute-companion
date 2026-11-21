"""Load, validate and overlap-check profiles.yaml. Reports every error, never stops at the first."""

import re
from collections.abc import Mapping, Sequence
from datetime import date, time, timedelta
from itertools import islice
from typing import Any, cast, get_args

import yaml

from train_commute_companion.calendar import (
    MINUTES_PER_DAY,
    WEEKDAYS,
    minute_of_day,
    nl_public_holidays,
)
from train_commute_companion.model import (
    DEFAULT_PREFER_DIRECT_MIN,
    MAX_TIMETABLE_HORIZON,
    AccessibilitySettings,
    AlertSettings,
    BriefSettings,
    Config,
    DayOverride,
    Direction,
    LocationSettings,
    LocationSource,
    Mode,
    NotificationCategory,
    NotificationChannel,
    Pick,
    Profile,
    ProfileProblem,
    Stations,
    TimetableHorizonUnit,
    UsualJourney,
    WorksDigestSettings,
    resolve_day,
)
from train_commute_companion.notification_policy import validate_channels
from train_commute_companion.overlap import overlap_pairs
from train_commute_companion.recurrence import (
    MAX_TRIPS,
    MonthlyWeekday,
    RepeatSchedule,
    recurring_dates,
)
from train_commute_companion.schedule import ACTIVE_EXTRA_MIN
from train_commute_companion.transfer import FALLBACK_MIN

ALLOWED_TRAVELLER = "me"
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_SOURCES: tuple[LocationSource, ...] = get_args(LocationSource)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# room for the -3..+3 day scan and next-day returns without date overflow
_DATE_MIN, _DATE_MAX = date(2, 1, 1), date(9998, 12, 31)


class _Loader(yaml.SafeLoader):
    """Leaves YAML timestamps as text, so _date validates every configured date."""

    def get_single_data(self) -> Any:
        node = self.get_single_node()
        if node is None:
            return None
        for _, profiles in _settings(node, {"profiles"}, unique=False):
            if isinstance(profiles, yaml.SequenceNode):
                for profile in profiles.value:
                    for key, settings in _settings(profile, {"brief", "alerts"}):
                        fields = (
                            {"at", "evening_at", "only_if_something"}
                            if key == "brief"
                            else {"daily_brief"}
                        )
                        _settings(settings, fields)
        return self.construct_document(node)


def _settings(
    node: yaml.Node, fields: set[str], *, unique: bool = True
) -> list[tuple[str, yaml.Node]]:
    """Check explicit settings keys before PyYAML flattens anchors and merge overrides."""
    pending = [node]
    visited: set[yaml.Node] = set()
    found = []
    while pending:
        mapping = pending.pop()
        if not isinstance(mapping, yaml.MappingNode) or mapping in visited:
            continue
        visited.add(mapping)
        seen = set()
        for key, value in mapping.value:
            if key.tag == "tag:yaml.org,2002:merge":
                pending.extend(value.value if isinstance(value, yaml.SequenceNode) else [value])
            if not isinstance(key, yaml.ScalarNode) or key.tag not in (
                "tag:yaml.org,2002:str",
                "tag:yaml.org,2002:value",
                "tag:yaml.org,2002:timestamp",
            ):
                continue
            if key.value in fields:
                if unique and key.value in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate {key.value!r} setting", key.start_mark
                    )
                seen.add(key.value)
                found.append((key.value, value))
    return found


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


def station_code(known: frozenset[str] | None, v: str) -> str:
    """The code of a station given by code or by an unambiguous name; else `v` unchanged."""
    found = known.lookup(v) if isinstance(known, Stations) else ()
    return found[0] if len(found) == 1 else v


def _station(c: _ProfileErrors, v: object, path: str, known: frozenset[str] | None) -> str | None:
    if not isinstance(v, str) or not v.strip():
        c.err(path, f"missing or not a station code: {v!r}")
        return None
    if known is None or v in known:
        return v
    if not isinstance(known, Stations):  # codes only: no name is known
        known = Stations(dict.fromkeys(known, ()))
    found = known.lookup(v)
    if len(found) == 1:
        return found[0]
    if found:
        listed = ", ".join(f"{code} ({known.name(code)})" for code in sorted(found))
        c.err(path, f"{v!r} names more than one station: {listed}", "use the station code")
        return None
    close = v.strip().upper()
    close = close if close in known else known.closest(v) or ""
    hint = f"did you mean {close!r}?" if close else "use a code from the NS station list"
    c.err(path, f"unknown station code {v!r}", hint)
    return None


def _transfers(
    c: _ProfileErrors, raw: object, name: str, known: frozenset[str] | None
) -> list[str]:
    if not isinstance(raw, list):
        c.err(f"{name}.transfers", "must be a list of station codes, [] for a direct train")
        return []
    found = (_station(c, t, f"{name}.transfers[{i}]", known) for i, t in enumerate(raw))
    return [s for s in found if s]


def _other_stations(
    c: _ProfileErrors, raw: object, name: str, known: frozenset[str] | None
) -> list[str]:
    if not isinstance(raw, list):
        c.err(f"{name}.other_stations", "must be a list of station codes, [] for none")
        return []
    path = f"{name}.other_stations"
    found = (_station(c, t, f"{path}[{i}]", known) for i, t in enumerate(raw))
    return [s for s in found if s]


def _check_other_stations(c: _ProfileErrors, p: Profile, has_location_block: bool) -> None:
    """Other boarding stations: never where the direction starts or ends, and only with a
    location source that can see the phone there."""
    for d in Direction:
        name = "outbound" if d is Direction.OUTBOUND else "return"
        others = p.journey(d).other_stations
        for i, code in enumerate(others):
            if code == p.origin(d):
                c.err(
                    f"{name}.other_stations[{i}]", f"{code} is already where this direction starts"
                )
            elif code == p.destination(d):
                c.err(f"{name}.other_stations[{i}]", f"{code} is where this direction ends")
        if others and not has_location_block:
            c.err(
                f"{name}.other_stations",
                "other_stations need a location block",
                "add location, or remove other_stations",
            )


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
    elif before < (need := buf + ACTIVE_EXTRA_MIN):
        c.err(
            f"{name}.window",
            f"window opens {before} min before first_train, needs at least {need}"
            f" (buffer_min + {ACTIVE_EXTRA_MIN})",
            f"open the window at {_shift(first, -need):%H:%M} or earlier",
        )


_MODES: tuple[Mode, ...] = get_args(Mode)
_PICKS: tuple[Pick, ...] = get_args(Pick)
_ARRIVAL_KEYS = ("arrive_by", "walk_after_min", "prefer_direct_min")
_DAY_KEYS = ("mode", "window", "pick", "min_stay_min", *_ARRIVAL_KEYS)
_NEEDS_WINDOW = "mode window needs a window: the earliest and latest departure"
_WINDOW_EXAMPLE = 'for example ["07:00", "10:00"]'
_EMPTY_WINDOW = "window is empty: the latest time must differ from the earliest"


def _mode(c: _ProfileErrors, v: object, path: str) -> Mode | None:
    found = next((m for m in _MODES if m == v), None)
    if found is None:
        c.err(path, f"must be fixed or window, got {v!r}")
    return found


def _pick(c: _ProfileErrors, v: object, path: str) -> Pick | None:
    found = next((m for m in _PICKS if m == v), None)
    if found is None:
        c.err(path, f"must be earliest, earliest_silent, on_leave or arrive_by, got {v!r}")
    return found


def _stay(c: _ProfileErrors, v: object, path: str, low: int = 1) -> int | None:
    """`low` 0 for a day: 0 turns off the stay the day would inherit."""
    if isinstance(v, int) and not isinstance(v, bool) and low <= v <= MINUTES_PER_DAY:
        return v
    c.err(path, f"must be a whole number of minutes from {low} to 1440, got {v!r}")
    return None


def _range(c: _ProfileErrors, raw: object, name: str) -> tuple[time, time] | None:
    """A window-mode window: the earliest and latest departure, never empty."""
    window = _window(c, raw, name, None)
    if window is not None and window[0] == window[1]:
        c.err(f"{name}.window", _EMPTY_WINDOW)
        return None
    return window


def _day_override(c: _ProfileErrors, raw: object, path: str) -> DayOverride | None:
    if not isinstance(raw, dict):
        c.err(path, "must be a mapping of weekday schedule settings")
        return None
    if "first_train" in raw:  # a likely first try: point to where a day's first train lives
        day, name = path.rsplit(".", 1)[1], path.split(".by_weekday")[0]
        c.err(
            f"{path}.first_train",
            "a day's first train is set in first_train_by_weekday",
            f'set {name}.first_train_by_weekday.{day}: "{raw["first_train"]}"',
        )
    c.keys({k: v for k, v in raw.items() if k != "first_train"}, _DAY_KEYS, path)
    return DayOverride(
        _mode(c, raw["mode"], f"{path}.mode") if "mode" in raw else None,
        _range(c, raw["window"], path) if "window" in raw else None,
        _pick(c, raw["pick"], f"{path}.pick") if "pick" in raw else None,
        _stay(c, raw["min_stay_min"], f"{path}.min_stay_min", 0) if "min_stay_min" in raw else None,
        c.time(raw["arrive_by"], f"{path}.arrive_by") if "arrive_by" in raw else None,
        _stay(c, raw["walk_after_min"], f"{path}.walk_after_min", 0)
        if "walk_after_min" in raw
        else None,
        _stay(c, raw["prefer_direct_min"], f"{path}.prefer_direct_min", 0)
        if "prefer_direct_min" in raw
        else None,
    )


def _by_weekday(c: _ProfileErrors, raw: object, name: str) -> dict[str, DayOverride]:
    if not isinstance(raw, dict):
        c.err(f"{name}.by_weekday", "must map mon through sun to overrides")
        return {}
    c.keys(raw, WEEKDAYS, f"{name}.by_weekday")
    out = {}
    for day, value in raw.items():
        o = _day_override(c, value, f"{name}.by_weekday.{day}")
        if o is not None and day in WEEKDAYS:
            out[day] = o
    return out


def _check_days(c: _ProfileErrors, j: UsualJourney, name: str, raw: Mapping[str, Any]) -> None:
    """Every weekday as it resolves, judged by the same `resolve_day` the runtime uses."""
    any_window = False
    any_arrive_by = False
    missing_targets: set[str] = set()
    for day in WEEKDAYS:
        r = resolve_day(j, day, True)
        o = j.by_weekday.get(day)
        at = f"{name}.by_weekday.{day}"
        if r.mode == "window" and r.pick == "arrive_by":
            any_arrive_by = True
            if r.arrive_by is None:
                missing_targets.add(f"{at if o and o.pick == 'arrive_by' else name}.arrive_by")
        elif o is not None:
            for key in _ARRIVAL_KEYS:
                if getattr(o, key) is not None:
                    c.err(f"{at}.{key}", f"{key} needs mode window and pick arrive_by")
        if r.mode == "window":
            any_window = True
            if o is not None and o.window is None and j.mode == "fixed":
                c.err(f"{at}.window", _NEEDS_WINDOW, _WINDOW_EXAMPLE)
            continue
        if o is None:
            continue
        if not r.first_train_set:
            c.err(
                f"{at}.mode",
                f"mode fixed needs a first train on {day}",
                f"set {name}.first_train_by_weekday.{day} or {name}.first_train",
            )
        elif o.window is not None or j.mode == "window":
            _check_window(c, at, r.first_train, r.window, j.buffer_min)
        for key in ("pick", "min_stay_min"):
            if getattr(o, key) not in (None, 0):  # a stay of 0 is off, also on a fixed day
                c.err(f"{at}.{key}", f"{key} has no effect on a fixed day")
    for key in ("pick", "min_stay_min"):
        if key in raw and not any_window:
            c.err(
                f"{name}.{key}",
                f"{key} has no effect: no day uses mode window",
                f"set mode: window, or remove {key}",
            )
    if not any_arrive_by:
        for key in _ARRIVAL_KEYS:
            if key in raw:
                c.err(f"{name}.{key}", f"{key} needs a window day with pick arrive_by")
    for path in sorted(missing_targets):
        if not any(e.path == f"{c.base}.{path}" for e in c.errors):
            c.err(path, 'pick arrive_by needs an arrival time, for example "09:00"')


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
            "mode",
            "first_train",
            "first_train_by_weekday",
            "transfers",
            "window",
            "pick",
            "min_stay_min",
            *_ARRIVAL_KEYS,
            "by_weekday",
            "buffer_min",
            "earlier_train_advice",
            "day_offset",
            "active_lead_min",
            "other_stations",
        ),
        name,
    )
    if "day_offset" in raw and not allow_offset:
        c.err(
            f"{name}.day_offset", "day_offset is allowed on return only", "remove it from outbound"
        )
    mode = _mode(c, raw.get("mode", "fixed"), f"{name}.mode")
    windowed = mode == "window"
    has_first = not windowed or raw.get("first_train") is not None
    first = c.time(raw.get("first_train"), f"{name}.first_train") if has_first else None
    transfers = _transfers(c, raw.get("transfers", []), name, known)
    others = _other_stations(c, raw.get("other_stations", []), name, known)
    buf = c.integer(raw.get("buffer_min"), f"{name}.buffer_min", 0)
    advice = c.boolean(
        raw.get("earlier_train_advice", name == "return"), f"{name}.earlier_train_advice"
    )
    offset = 0
    if allow_offset:
        offset = c.integer(raw.get("day_offset", 0), f"{name}.day_offset", 0, 1) or 0
    lead = None
    if "active_lead_min" in raw:
        lead = c.integer(raw["active_lead_min"], f"{name}.active_lead_min", 0)
        if lead is not None and buf is not None and lead < buf:
            c.err(
                f"{name}.active_lead_min",
                f"must be at least buffer_min ({buf}): everything is told from the moment"
                " you need to leave",
                f"set it to {buf + ACTIVE_EXTRA_MIN} or remove it for the default",
            )
    if windowed:
        window = _range(c, raw.get("window"), name)
        if raw.get("window") is None:
            c.err(f"{name}.window", _NEEDS_WINDOW, _WINDOW_EXAMPLE)
    else:
        window = _window(c, raw.get("window"), name, first)
        if first and window and buf is not None:
            _check_window(c, name, first, window, buf)
    pick = _pick(c, raw["pick"], f"{name}.pick") if "pick" in raw else "earliest"
    stay = _stay(c, raw["min_stay_min"], f"{name}.min_stay_min") if "min_stay_min" in raw else None
    days = _by_weekday(c, raw.get("by_weekday", {}), name)
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
    if window is not None and first is None and windowed:
        first = window[
            0
        ]  # window mode: no usual train; window start stands in for code that needs a time
    if mode is None or first is None or window is None or buf is None or advice is None:
        return None
    j = UsualJourney(
        first,
        tuple(transfers),
        window,
        buf,
        advice,
        offset,
        weekday_times,
        lead,
        mode,
        pick or "earliest",  # a bad pick is already reported; the days are still checked
        stay,
        days,
        has_first,
        tuple(others),
        arrive_by=c.time(raw["arrive_by"], f"{name}.arrive_by") if "arrive_by" in raw else None,
        walk_after_min=_stay(c, raw.get("walk_after_min", 0), f"{name}.walk_after_min", 0) or 0,
        prefer_direct_min=(
            _stay(
                c,
                raw.get("prefer_direct_min", DEFAULT_PREFER_DIRECT_MIN),
                f"{name}.prefer_direct_min",
                0,
            )
            or 0
        ),
    )
    _check_days(c, j, name, raw)
    return None if len(c.errors) > n else j


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
            "compensation_details",
            "timetable_update",
            "arrival_prompt",
            "navigate",
            "quiet_repeat_misses",
            "weather",
            "delay_min",
            "delay_step_min",
            "leave_by",
            "weather_allowance",
            "custom_timetable_horizon",
            "timetable_horizon_unit",
            "timetable_horizon_count",
            "live_today",
            "telegram_status_pin",
            "ntfy_enabled",
            "arrive_by",
            "commute_stats",
            "weekly_recap",
            "after_trip_note",
            "alert_history",
            "calendar_feed",
            "quick_trip",
            "daily_brief",
            "location_leave_now",
        ),
        "alerts",
    )
    d = AlertSettings()
    history_switches = {
        key: c.boolean(raw.get(key, getattr(d, key)), f"alerts.{key}")
        for key in ("commute_stats", "weekly_recap", "after_trip_note")
    }
    quick_trip = c.boolean(raw.get("quick_trip", d.quick_trip), "alerts.quick_trip")
    daily_brief = c.boolean(raw.get("daily_brief", d.daily_brief), "alerts.daily_brief")
    custom_horizon = c.boolean(
        raw.get("custom_timetable_horizon", d.custom_timetable_horizon),
        "alerts.custom_timetable_horizon",
    )
    horizon_unit = raw.get("timetable_horizon_unit", d.timetable_horizon_unit)
    if horizon_unit not in get_args(TimetableHorizonUnit):
        c.err("alerts.timetable_horizon_unit", "must be calendar_days or commute_dates")
    horizon_count = raw.get("timetable_horizon_count", d.timetable_horizon_count)
    if type(horizon_count) is not int or not 1 <= horizon_count <= MAX_TIMETABLE_HORIZON:
        c.err(
            "alerts.timetable_horizon_count",
            f"must be a whole number from 1 to {MAX_TIMETABLE_HORIZON}",
        )
    weather = raw.get("weather", True)
    if not isinstance(weather, bool):
        c.err("alerts.weather", "must be true or false")
    delays = {}
    for key in ("delay_min", "delay_step_min"):
        value = raw.get(key, getattr(d, key))
        if type(value) is not int or not 1 <= value <= 1440:
            c.err(f"alerts.{key}", "must be a whole number of minutes from 1 to 1440")
        else:
            delays[key] = value
    leave = c.boolean(raw.get("leave_by", d.leave_by), "alerts.leave_by")
    allowance = c.boolean(
        raw.get("weather_allowance", d.weather_allowance), "alerts.weather_allowance"
    )
    live_today = c.boolean(raw.get("live_today", d.live_today), "alerts.live_today")
    pin = c.boolean(
        raw.get("telegram_status_pin", d.telegram_status_pin), "alerts.telegram_status_pin"
    )
    ntfy = c.boolean(raw.get("ntfy_enabled", d.ntfy_enabled), "alerts.ntfy_enabled")
    arrive = c.boolean(raw.get("arrive_by", d.arrive_by), "alerts.arrive_by")
    history = c.boolean(raw.get("alert_history", d.alert_history), "alerts.alert_history")
    calendar_feed = c.boolean(raw.get("calendar_feed", d.calendar_feed), "alerts.calendar_feed")
    location_leave = c.boolean(
        raw.get("location_leave_now", d.location_leave_now), "alerts.location_leave_now"
    )
    compensation = raw.get("compensation_reminder", False)
    if not isinstance(compensation, bool):
        c.err("alerts.compensation_reminder", "must be true or false")
    compensation_details = c.boolean(
        raw.get("compensation_details", d.compensation_details), "alerts.compensation_details"
    )
    timetable_update = c.boolean(
        raw.get("timetable_update", d.timetable_update), "alerts.timetable_update"
    )
    arrival_prompt = c.boolean(raw.get("arrival_prompt", d.arrival_prompt), "alerts.arrival_prompt")
    navigate = c.boolean(raw.get("navigate", d.navigate), "alerts.navigate")
    repeat = raw.get("quiet_repeat_misses", False)
    if not isinstance(repeat, bool):
        c.err("alerts.quiet_repeat_misses", "must be true or false")
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
    if (
        len(c.errors) > n
        or thr is None
        or sav is None
        or leave is None
        or allowance is None
        or custom_horizon is None
        or live_today is None
        or pin is None
        or ntfy is None
        or arrive is None
        or history is None
        or calendar_feed is None
        or quick_trip is None
        or daily_brief is None
        or location_leave is None
        or arrival_prompt is None
        or compensation_details is None
        or timetable_update is None
        or navigate is None
    ):
        return None
    return AlertSettings(
        thr,
        sav,
        late,
        quiet,
        frozenset(cast(list[NotificationCategory], disabled)),
        channels,
        compensation,
        repeat,
        weather=weather,
        delay_min=delays["delay_min"],
        delay_step_min=delays["delay_step_min"],
        leave_by=leave,
        weather_allowance=allowance,
        custom_timetable_horizon=custom_horizon,
        timetable_horizon_unit=cast(TimetableHorizonUnit, horizon_unit),
        timetable_horizon_count=horizon_count,
        live_today=live_today,
        telegram_status_pin=pin,
        ntfy_enabled=ntfy,
        arrive_by=arrive,
        commute_stats=cast(bool, history_switches["commute_stats"]),
        weekly_recap=cast(bool, history_switches["weekly_recap"]),
        after_trip_note=cast(bool, history_switches["after_trip_note"]),
        alert_history=history,
        calendar_feed=calendar_feed,
        quick_trip=quick_trip,
        daily_brief=daily_brief,
        location_leave_now=location_leave,
        arrival_prompt=arrival_prompt,
        compensation_details=compensation_details,
        timetable_update=timetable_update,
        navigate=navigate,
    )


def _location(c: _ProfileErrors, raw: object) -> tuple[bool, LocationSettings | None]:
    if raw is None:
        return True, None
    if not isinstance(raw, dict):
        c.err("location", "must be a mapping or null")
        return False, None
    n = len(c.errors)
    keys = (
        "source",
        "enabled",
        "entity",
        "zones",
        "station_radius_m",
        "wifi",
        "activity_entity",
        "other_station_radius_m",
        "other_station_dwell_min",
        "ask_to_remember_station",
    )
    c.keys(raw, keys, "location")
    src, ent, zones = raw.get("source"), raw.get("entity"), raw.get("zones", {})
    wifi, activity = raw.get("wifi", {}), raw.get("activity_entity")
    radius = raw.get("station_radius_m", 150)
    if isinstance(radius, bool) or not isinstance(radius, int) or radius <= 0:
        c.err("location.station_radius_m", "must be a whole number of metres above 0")
    source = next((s for s in _SOURCES if s == src), None)
    enabled = c.boolean(raw.get("enabled", True), "location.enabled")
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
    if not isinstance(wifi, dict) or not all(
        k in ("home", "work") and isinstance(v, str) and v for k, v in wifi.items()
    ):
        c.err(
            "location.wifi",
            "must map home or work to a Wi-Fi tracker entity",
            "for example home: device_tracker.phone_wifi",
        )
    if activity is not None and (not isinstance(activity, str) or not activity):
        c.err(
            "location.activity_entity", "must be an entity id", "for example sensor.phone_activity"
        )
    if (wifi or activity) and src != "ha":
        c.err("location", "wifi and activity_entity need source ha")
    other_radius = raw.get("other_station_radius_m", 500)
    if not _whole(other_radius, 1, 5000):
        c.err("location.other_station_radius_m", "must be a whole number of metres from 1 to 5000")
    dwell = raw.get("other_station_dwell_min", 0)
    if not _whole(dwell, 0, 60):
        c.err("location.other_station_dwell_min", "must be a whole number of minutes from 0 to 60")
    ask = c.boolean(raw.get("ask_to_remember_station", True), "location.ask_to_remember_station")
    if source is None or enabled is None or ask is None or len(c.errors) > n:
        return False, None
    if not enabled:
        return True, None
    return True, LocationSettings(
        source, ent, dict(zones), radius, dict(wifi), activity, other_radius, dwell, ask
    )


def _whole(v: object, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


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


def _repeat(
    c: _ProfileErrors, raw: object, days: set[int], skip_holidays: bool | None
) -> RepeatSchedule | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        c.err("repeat", "must be a mapping")
        return None
    before = len(c.errors)
    c.keys(raw, ("start_date", "end_date", "end_after", "every_weeks", "monthly"), "repeat")
    interval = c.integer(raw.get("every_weeks", 1), "repeat.every_weeks", 1, 52)
    start = (
        _date(c, raw["start_date"], "repeat.start_date")
        if raw.get("start_date") is not None
        else date(2, 1, 7)
        if interval and interval > 1
        else _DATE_MIN
    )
    end = _date(c, raw["end_date"], "repeat.end_date") if raw.get("end_date") is not None else None
    if not days:
        c.err("repeat", "choose at least one day")
    if start is not None and end is not None and end < start:
        c.err("repeat.end_date", "must be on or after the start date")
    ordinal = raw.get("monthly")
    month = None
    if ordinal is not None:
        ordinal = c.integer(ordinal, "repeat.monthly", 1, 5)
        if len(days) != 1 or interval != 1:
            c.err("repeat.monthly", "choose one weekday and clear the interval in weeks")
        elif ordinal is not None:
            month = MonthlyWeekday(ordinal, next(iter(days)))
    count = raw.get("end_after")
    if count is not None:
        if raw.get("start_date") is None:
            c.err("repeat.start_date", "an explicit start date is required with end_after")
        if raw.get("end_date") is not None:
            c.err("repeat.end_after", "choose an end date or a trip count")
        count = c.integer(count, "repeat.end_after", 1, MAX_TRIPS)
    if start is None or interval is None or len(c.errors) != before:
        return None
    if count is not None:
        dates = recurring_dates(start, _DATE_MAX, start, frozenset(days), interval, month)
        eligible = (
            day for day in dates if not skip_holidays or day not in nl_public_holidays(day.year)
        )
        end = next(islice(eligible, count - 1, None), None)
        if end is None:
            c.err("repeat.end_after", "trip count extends beyond the supported calendar")
            return None
    return RepeatSchedule(start, end, interval, month, count)


def _transfer_min(
    c: _ProfileErrors, raw: Mapping[str, object], known: frozenset[str] | None
) -> dict[str, int]:
    """Reads the raw journeys, so a transfer station of a journey that failed to parse is still
    checked and every error comes out in one pass. Keys and transfers given by name are compared
    as codes; a bad name is already reported where it is used."""
    tm_raw = raw.get("transfer_min", {})
    tm: dict[str, int] = {}
    if not isinstance(tm_raw, dict):
        c.err("transfer_min", "must be a mapping such as { UT: 4 }")
        return tm
    seen: dict[str, object] = {}
    for k, v in tm_raw.items():
        code = station_code(known, str(k))
        if code in seen:
            c.err(
                f"transfer_min.{k}",
                f"{k} and {seen[code]} are the same station {code}",
                "keep one of them",
            )
        seen[code] = k
        iv = c.integer(v, f"transfer_min.{k}", 1)
        if iv is not None:
            tm[code] = iv
    transfers: list[str] = []
    for key in ("outbound", "return"):
        j = raw.get(key)
        tl = j.get("transfers") if isinstance(j, dict) else None
        transfers += [s for s in (tl if isinstance(tl, list) else ()) if isinstance(s, str)]
    if known is None and any(k not in transfers for k in tm_raw):
        return tm  # unchecked: a spare key may be a transfer's other spelling, name or code (5.3)
    for s in transfers:
        if station_code(known, s) not in seen:
            c.err(
                f"transfer_min.{s}",
                f"no transfer_min for transfer station {s}",
                f"add transfer_min: {{ {s}: {FALLBACK_MIN} }} ({FALLBACK_MIN} min is the fallback)",
            )
    return tm


_PROFILE_KEYS = (
    "id",
    "name",
    "traveller",
    "days",
    "repeat",
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
    "brief",
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


def _brief(c: _ProfileErrors, raw: object) -> BriefSettings:
    defaults = BriefSettings()
    if not isinstance(raw, dict):
        c.err("brief", "must be a mapping")
        return defaults
    c.keys(raw, ("at", "evening_at", "only_if_something"), "brief")
    at = c.time(raw["at"], "brief.at") if "at" in raw else None
    evening = c.time(raw["evening_at"], "brief.evening_at") if "evening_at" in raw else None
    only = c.boolean(raw.get("only_if_something", False), "brief.only_if_something")
    return BriefSettings(at, evening or defaults.evening_at, only is True)


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
    repeat = _repeat(c, raw.get("repeat"), days, skip)
    home = _station(c, raw.get("home_station"), "home_station", known)
    work = _station(c, raw.get("work_station"), "work_station", known)
    out = _journey(c, raw.get("outbound"), "outbound", known, allow_offset=False)
    ret = _journey(c, raw.get("return"), "return", known, allow_offset=True)
    tm = _transfer_min(c, raw, known)
    alerts = _alerts(c, raw.get("alerts"))
    loc_ok, loc = _location(c, raw.get("location"))
    accessibility = _accessibility(c, raw.get("accessibility", {}))
    digest = _works_digest(c, raw.get("works_digest"))
    brief = _brief(c, raw.get("brief", {}))
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
    profile = Profile(
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
        repeat=repeat,
        brief=brief,
    )
    _check_other_stations(c, profile, raw.get("location") is not None)
    return (None, c.errors) if c.errors else (profile, [])


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
    except (KeyError, IndexError):
        return None, [
            ProfileProblem(None, "file", "invalid tagged YAML value", "fix the YAML syntax")
        ]
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


def _explicit_empty_profiles(text: str) -> bool:
    # A profile reset must come from the file itself, before YAML merges and tag conversion.
    root = yaml.compose(text.lstrip("\ufeff"), Loader=_Loader)
    if not isinstance(root, yaml.MappingNode) or root.tag != "tag:yaml.org,2002:map":
        return False
    if len(root.value) != 1:
        return False
    key, value = root.value[0]
    return (
        key.tag == "tag:yaml.org,2002:str"
        and key.value == "profiles"
        and isinstance(value, yaml.SequenceNode)
        and value.tag == "tag:yaml.org,2002:seq"
        and not value.value
    )


def parse_profiles(
    text: str,
    known_stations: frozenset[str] | None,
    *,
    available_channels: frozenset[NotificationChannel] | None = None,
    allow_empty: bool = False,
) -> tuple[list[Profile], list[ProfileProblem]]:
    data, errors = _load_yaml(text)
    if errors:
        return [], errors
    items = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(items, list) or (
        not items and not (allow_empty and _explicit_empty_profiles(text))
    ):
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
    profiles, errors = parse_profiles(
        text,
        known_stations,
        available_channels=available_channels,
        allow_empty=previous is not None,
    )
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
        if j.arrive_by is not None:
            out.append((f"{d.value}.arrive_by", j.arrive_by))
        for weekday, o in j.by_weekday.items():
            if o.arrive_by is not None:
                out.append((f"{d.value}.by_weekday.{weekday}.arrive_by", o.arrive_by))
            out += [
                (f"{d.value}.by_weekday.{weekday}.window[{i}]", t)
                for i, t in enumerate(o.window or ())
            ]
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
    """`load_config` as readable lines: every error, then the warnings: home and work the same
    station, and times in the clock-change hour (5.3)."""
    cfg, errors = load_config(text, known, None, today)
    lines = [
        f"{e.path}: {e.message}" + (f" ({e.suggestion})" if e.suggestion else "") for e in errors
    ]
    for p in cfg.profiles if cfg else ():
        # a warning, not an error: older versions saved such profiles, and the file must
        # still load so the profile editor can fix it (the editor form refuses it)
        if p.home_station == p.work_station:
            lines.append(
                f"warning: {p.name}.work_station: {p.work_station} is also the home station "
                "(pick two different stations)"
            )
        for where, t in _times(p):
            if t.hour == _DST_HOUR:
                lines.append(
                    f"warning: {p.name}.{where}: {t:%H:%M} is between {_DST_HOUR:02d}:00 and "
                    f"{_DST_HOUR + 1:02d}:00, which is repeated or skipped on the clock-change nights"
                )
    return cfg, lines

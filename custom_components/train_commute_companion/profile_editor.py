"""Edit portable profiles without losing advanced settings or another editor's changes."""

import contextlib
import copy
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from train_commute_companion.calendar import WEEKDAYS
from train_commute_companion.model import (
    DEFAULT_PREFER_DIRECT_MIN,
    AlertSettings,
    Config,
    NotificationChannel,
)
from train_commute_companion.notification_policy import CATEGORIES
from train_commute_companion.profile import load_config, station_code
from train_commute_companion.transfer import FALLBACK_MIN

from .store import file_lock

REPEAT_FIELDS = ("start_date", "end_date", "every_weeks", "monthly", "end_after")


class EditorError(ValueError):
    def __init__(
        self, code: str, report: str = "", override_days: frozenset[tuple[str, str]] = frozenset()
    ) -> None:
        super().__init__(code)
        self.code, self.report, self.override_days = code, report, override_days


@dataclass(frozen=True, slots=True)
class ProfileDocument:
    path: Path
    content: bytes | None
    data: dict[str, Any]


def _allowed(path: Path, roots: tuple[Path, ...]) -> Path:
    try:
        resolved = path.resolve()
        allowed = any(resolved.is_relative_to(root.resolve()) for root in roots)
    except (OSError, ValueError):
        raise EditorError("path_not_allowed") from None
    if not allowed:
        raise EditorError("path_not_allowed")
    return resolved


def validate_text(
    text: str,
    known: frozenset[str],
    today: date,
    available_channels: frozenset[NotificationChannel] | None = None,
    *,
    profile_index: int | None = None,
) -> Config:
    config, errors = load_config(text, known, None, today, available_channels=available_channels)
    if config is None or errors:
        paths = [error.path.split(".") for error in errors]
        override_days: frozenset[tuple[str, str]] = frozenset()
        if profile_index is not None and all(
            len(path) >= 4
            and path[0] == f"profiles[{profile_index}]"
            and path[1] in ("outbound", "return")
            and path[2] == "by_weekday"
            and path[3] in WEEKDAYS
            for path in paths
        ):
            override_days = frozenset((path[1], path[3]) for path in paths)
        raise EditorError(
            "invalid_profile",
            "\n".join(f"{e.path}: {e.message}" for e in errors),
            override_days,
        )
    return config


def read_document(
    path: Path, roots: tuple[Path, ...], known: frozenset[str], today: date, *, create: bool = False
) -> ProfileDocument:
    resolved = _allowed(path, roots)
    try:
        content = resolved.read_bytes()
    except FileNotFoundError:
        if create:
            return ProfileDocument(resolved, None, {"profiles": []})
        raise EditorError("file_missing") from None
    except OSError:
        raise EditorError("file_unreadable") from None
    if create:
        raise EditorError("file_changed")
    try:
        text = content.decode("utf-8-sig")
        validate_text(text, known, today)
        data = yaml.safe_load(text)
    except (UnicodeError, yaml.YAMLError):
        raise EditorError("invalid_profile") from None
    return ProfileDocument(resolved, content, data)


def edit_profile(
    document: ProfileDocument,
    profile_id: str | None,
    values: Mapping[str, Any],
    known: frozenset[str],
    today: date,
    *,
    available_channels: frozenset[NotificationChannel] | None = None,
) -> str:
    data = copy.deepcopy(document.data)
    index: int | None
    if profile_id is None:
        index = len(data["profiles"])
        profile: dict[str, Any] = {"id": uuid4().hex}
        data["profiles"].append(profile)
    else:
        index = next((i for i, p in enumerate(data["profiles"]) if p["id"] == profile_id), None)
        if index is None:
            raise EditorError("file_changed")
        # YAML aliases must not let this edit change another profile or section.
        profile = coded(data["profiles"][index], known)
        data["profiles"][index] = profile
        if "alerts" in profile and profile["alerts"] is None:
            profile["alerts"] = {}
    for key in ("name", "days", "home_station", "work_station", "skip_public_holidays"):
        profile[key] = values[key]
    if any(f"repeat_{key}" in values for key in REPEAT_FIELDS):
        repeat = {}
        for key in REPEAT_FIELDS:
            value = values.get(f"repeat_{key}")
            if value is not None and value != "":
                repeat[key] = (
                    int(value) if isinstance(value, float) and value.is_integer() else value
                )
        if repeat:
            profile["repeat"] = repeat
        else:
            profile.pop("repeat", None)
    if "require_accessible" in values:
        profile["accessibility"] = {
            **copy.deepcopy(profile.get("accessibility", {})),
            "require_accessible": values["require_accessible"],
        }
    if "disabled_categories" in values:
        profile["alerts"] = {
            **copy.deepcopy(profile.get("alerts", {})),
            "disabled_categories": values["disabled_categories"],
        }
    for key in ("delay_min", "delay_step_min"):
        if key in values:
            value = values[key]
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            alerts = copy.deepcopy(profile.get("alerts", {}))
            if value != 3 or key in alerts:
                profile["alerts"] = {**alerts, key: value}
    defaults = AlertSettings()
    for key in (
        "compensation_reminder",
        "quiet_repeat_misses",
        "weather",
        "leave_by",
        "weather_allowance",
        "location_leave_now",
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
    ):
        if key not in values:
            continue
        if type(values[key]) is not bool:
            raise EditorError("invalid_profile")
        alerts = copy.deepcopy(profile.get("alerts", {}))
        if values[key] != getattr(defaults, key) or key in alerts or key == "weather":
            alerts[key] = values[key]
            profile["alerts"] = alerts
    for key in ("custom_timetable_horizon", "timetable_horizon_unit", "timetable_horizon_count"):
        if key in values:
            value = values[key]
            if key == "timetable_horizon_count" and isinstance(value, float) and value.is_integer():
                value = int(value)
            alerts = copy.deepcopy(profile.get("alerts", {}))
            default = getattr(defaults, key)
            if type(value) is not type(default) or value != default or key in alerts:
                profile["alerts"] = {**alerts, key: value}
    if "daily_brief" in values:
        value = values["daily_brief"]
        alerts = profile.get("alerts", {})
        if type(value) is not bool:
            raise EditorError("invalid_profile")
        if value is not defaults.daily_brief or "daily_brief" in alerts:
            profile["alerts"] = {**alerts, "daily_brief": value}
    brief = dict(profile.get("brief", {}))
    for key in ("at", "evening_at", "only_if_something"):
        field = f"brief_{key}"
        if field in values:
            value = values[field]
            if key == "only_if_something" and type(value) is not bool:
                raise EditorError("invalid_profile")
            if value == "" or value is False:
                brief.pop(key, None)
            else:
                brief[key] = value
    if brief:
        profile["brief"] = brief
    else:
        profile.pop("brief", None)
    if any(f"channel_{category}" in values for category in CATEGORIES):
        alerts = copy.deepcopy(profile.get("alerts", {}))
        channels = copy.deepcopy(alerts.get("channels", {}))
        for category in CATEGORIES:
            key = f"channel_{category}"
            if key not in values:
                continue
            value = values[key]
            if value == "primary_webhook":
                value = "primary,webhook"
            if value == "default":
                channels.pop(category, None)
            elif isinstance(value, str):
                channels[category] = value.split(",")
            else:
                raise EditorError("invalid_profile")
        if channels:
            alerts["channels"] = channels
        else:
            alerts.pop("channels", None)
        profile["alerts"] = alerts
    minimum = copy.deepcopy(profile.get("transfer_min", {}))
    profile["transfer_min"] = minimum
    for direction in ("outbound", "return"):
        journey = copy.deepcopy(profile.get(direction, {}))
        profile[direction] = journey
        _write_schedule(journey, direction, values)
        journey["transfers"] = values[f"{direction}_transfers"]
        buffer = values[f"{direction}_buffer"]
        if isinstance(buffer, float) and buffer.is_integer():
            buffer = int(buffer)
        journey["buffer_min"] = buffer
        lead_key = f"{direction}_active_lead"
        if lead_key in values:
            lead = values[lead_key]
            if lead is None:
                journey.pop("active_lead_min", None)
            else:
                if isinstance(lead, float) and lead.is_integer():
                    lead = int(lead)
                journey["active_lead_min"] = lead
        weekday_key = f"{direction}_weekday_times"
        if weekday_key in values:
            overrides = values[weekday_key]
            if not isinstance(overrides, dict):
                raise EditorError("invalid_profile")
            if overrides:
                journey["first_train_by_weekday"] = copy.deepcopy(overrides)
            else:
                journey.pop("first_train_by_weekday", None)
        days_key = f"{direction}_by_weekday"
        if days_key in values:
            days = values[days_key]
            if not isinstance(days, dict):
                raise EditorError("invalid_profile")
            if days:
                journey["by_weekday"] = copy.deepcopy(days)
            else:
                journey.pop("by_weekday", None)
        for station in journey["transfers"]:
            minimum.setdefault(station, FALLBACK_MIN)
    minimum.update(values.get("transfer_min", {}))
    _write_alert_schedule(profile, values)
    _write_ha_location(profile, values)
    _write_other_stations(profile, values)
    action = values.get("pause_action", "keep")
    if action not in ("keep", "add", "replace", "clear"):
        raise EditorError("invalid_profile")
    if action in ("replace", "clear"):
        profile["pauses"] = []
    if action in ("add", "replace"):
        profile["pauses"] = copy.deepcopy(profile.get("pauses", []))
        profile["pauses"].append(
            {"start": values.get("pause_start"), "end": values.get("pause_end")}
        )
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    validate_text(text, known, today, available_channels, profile_index=index)
    return text


def coded(profile: Mapping[str, Any], known: frozenset[str]) -> dict[str, Any]:
    """A copy of one profile with every station as its code, so a profile that names its
    stations fills the station fields and is saved with codes. Other profiles stay as written."""
    out = copy.deepcopy(dict(profile))

    def code(value: object) -> object:
        return station_code(known, value) if isinstance(value, str) else value

    for key in ("home_station", "work_station"):
        if key in out:
            out[key] = code(out[key])
    for direction in ("outbound", "return"):
        journey = out.get(direction)
        for key in ("transfers", "other_stations"):
            if isinstance(journey, dict) and isinstance(journey.get(key), list):
                journey[key] = [code(s) for s in journey[key]]
    if isinstance(out.get("transfer_min"), dict):  # the file was checked: no two keys collide
        out["transfer_min"] = {code(k): v for k, v in out["transfer_min"].items()}
    return out


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _form_time(value: object, path: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise EditorError("invalid_profile", f"{path}: use HH:MM from 00:00 to 23:59")
    return value


def _write_alert_schedule(profile: dict[str, Any], values: Mapping[str, Any]) -> None:
    if "quiet_start" in values or "quiet_end" in values:
        start, end = values.get("quiet_start", ""), values.get("quiet_end", "")
        alerts = dict(profile.get("alerts") or {})
        if start == end == "":
            alerts.pop("quiet_hours", None)
        else:
            quiet = [_form_time(t, "alerts.quiet_hours") for t in (start, end)]
            if quiet[0] == quiet[1]:
                raise EditorError("invalid_profile", "alerts.quiet_hours: use two different times")
            alerts["quiet_hours"] = quiet
        if alerts or "alerts" in profile:
            profile["alerts"] = alerts
    if "works_digest_day" in values or "works_digest_time" in values:
        day = values.get("works_digest_day")
        if day == "off":
            profile.pop("works_digest", None)
        elif day in WEEKDAYS:
            at = _form_time(values.get("works_digest_time"), "works_digest.time")
            profile["works_digest"] = {"day": day, "time": at}
        else:
            raise EditorError("invalid_profile", "works_digest.day: choose a weekday or Off")


def _write_ha_location(profile: dict[str, Any], values: Mapping[str, Any]) -> None:
    location = dict(profile.get("location") or {})
    if "ha_location" in values:
        selected = values["ha_location"]
        location.update(source="ha", entity=selected["entity"])
        location.pop("enabled", None)
        zones = dict(location.get("zones", {}))
        for role in ("home", "work"):
            zones.pop(role, None)
        for key, selection in (
            ("zones", {**zones, **selected["zones"]}),
            ("wifi", selected["wifi"]),
            ("activity_entity", selected["activity_entity"]),
        ):
            if selection:
                location[key] = selection
            else:
                location.pop(key, None)
        profile["location"] = location
    elif values.get("disable_ha_location") is True and location.get("source") == "ha":
        profile["location"] = {**location, "enabled": False}


def _write_schedule(journey: dict[str, Any], direction: str, values: Mapping[str, Any]) -> None:
    """The first train, mode, window, pick and minimum stay of one direction. A fixed profile
    keeps its shape: `mode`, `pick` and `min_stay_min` are written only when they matter."""
    first = _text(values.get(f"{direction}_time"))
    mode = values.get(f"{direction}_mode", journey.get("mode", "fixed"))
    if mode not in ("fixed", "window"):
        raise EditorError("invalid_profile")
    if mode == "window" or "mode" in journey:
        journey["mode"] = mode
    start, end = (_text(values.get(f"{direction}_window_{edge}")) for edge in ("start", "end"))
    if mode == "window" or start or end:
        journey["window"] = [start, end]
    elif f"{direction}_mode" in values:
        # the form showed the window and it came back blank: the default watch window
        journey.pop("window", None)
    if first or mode == "fixed":
        journey["first_train"] = first or values.get(f"{direction}_time")
    else:
        journey.pop("first_train", None)
    if f"{direction}_pick" in values:
        pick = values[f"{direction}_pick"]
        if pick == "earliest":
            journey.pop("pick", None)
        else:
            journey["pick"] = pick
    if f"{direction}_min_stay" in values:
        stay = values[f"{direction}_min_stay"]
        if isinstance(stay, float) and stay.is_integer():
            stay = int(stay)
        if stay:
            journey["min_stay_min"] = stay
        else:
            journey.pop("min_stay_min", None)
    _write_arrival(journey, values, f"{direction}_")


def _write_arrival(journey: dict[str, Any], values: Mapping[str, Any], prefix: str) -> None:
    fields = ("arrive_by", "walk_after_min", "prefer_direct_min")
    if not any(f"{prefix}{key}" in values for key in fields):
        return
    days = values.get(f"{prefix}by_weekday", journey.get("by_weekday", {}))
    uses_target = journey.get("pick") == "arrive_by" or any(
        isinstance(day, dict) and day.get("pick") == "arrive_by"
        for day in (days.values() if isinstance(days, dict) else ())
    )
    if not uses_target:
        for key in fields:
            journey.pop(key, None)
        return
    key = f"{prefix}arrive_by"
    if key in values:
        target = _text(values[key])
        if target:
            journey["arrive_by"] = target
        else:
            journey.pop("arrive_by", None)
    for key, default in (("walk_after_min", 0), ("prefer_direct_min", DEFAULT_PREFER_DIRECT_MIN)):
        field = f"{prefix}{key}"
        if field in values:
            value = values[field]
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if value is not None and (type(value) is not int or value != default or key in journey):
                journey[key] = value


OTHER_STATION_DEFAULTS = {
    "other_station_radius_m": 500,
    "other_station_dwell_min": 0,
    "ask_to_remember_station": True,
}


def _write_other_stations(profile: dict[str, Any], values: Mapping[str, Any]) -> None:
    """Other boarding stations per direction and their location settings. A default is written
    only when the key is already in the file, so an untouched profile keeps its shape."""
    for direction in ("outbound", "return"):
        key = f"{direction}_other_stations"
        if key not in values:
            continue
        codes = values[key]
        if not isinstance(codes, list):
            raise EditorError("invalid_profile")
        journey = profile[direction]
        if codes or "other_stations" in journey:
            journey["other_stations"] = list(codes)
    location = profile.get("location")
    if not isinstance(location, dict):
        return
    location = copy.deepcopy(location)
    for key, default in OTHER_STATION_DEFAULTS.items():
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if value != default or key in location:
            location[key] = value
    profile["location"] = location


class WritePhase(StrEnum):
    CHECKING = "checking"
    STAGED = "staged"
    COMMITTED = "committed"
    CLOSED = "closed"


class ProfileWrite:
    """Own the staged file until its snapshot comparison and atomic commit succeed."""

    def __init__(self, document: ProfileDocument, path: Path) -> None:
        self.document, self.path = document, path
        self.phase = WritePhase.CHECKING
        self.temporary: str | None = None
        self.created: list[Path] = []

    def _check_snapshot(self) -> None:
        if (self.path.read_bytes() if self.path.exists() else None) != self.document.content:
            raise EditorError("file_changed")

    def stage(self, text: str) -> None:
        if self.phase is not WritePhase.CHECKING:
            raise EditorError("save_failed")
        self._check_snapshot()
        parent = self.path.parent
        while not parent.exists():
            self.created.append(parent)
            parent = parent.parent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, self.temporary = tempfile.mkstemp(
            prefix=".profiles-", suffix=".tmp", dir=self.path.parent
        )
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        self.phase = WritePhase.STAGED

    def commit(self) -> None:
        if self.phase is not WritePhase.STAGED or self.temporary is None:
            raise EditorError("save_failed")
        self._check_snapshot()
        if self.document.content is None:
            os.link(self.temporary, self.path)
        else:
            os.replace(self.temporary, self.path)
            self.temporary = None
        self.phase = WritePhase.COMMITTED

    def close(self) -> None:
        if self.phase is WritePhase.CLOSED:
            return
        if self.temporary is not None:
            with contextlib.suppress(OSError):
                Path(self.temporary).unlink(missing_ok=True)
        if self.phase is not WritePhase.COMMITTED:
            for directory in self.created:
                with contextlib.suppress(OSError):
                    directory.rmdir()
        self.phase = WritePhase.CLOSED


def save_document(document: ProfileDocument, text: str, roots: tuple[Path, ...]) -> None:
    path = _allowed(document.path, roots)
    transaction = ProfileWrite(document, path)
    with file_lock(path):
        try:
            transaction.stage(text)
            transaction.commit()
        except FileExistsError:
            raise EditorError("file_changed") from None
        except OSError:
            raise EditorError("save_failed") from None
        finally:
            transaction.close()

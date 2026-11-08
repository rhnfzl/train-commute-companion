"""Edit portable profiles without losing advanced settings or another editor's changes."""

import contextlib
import copy
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from train_commute_companion.model import NotificationChannel
from train_commute_companion.notification_policy import CATEGORIES
from train_commute_companion.profile import load_config
from train_commute_companion.transfer import FALLBACK_MIN

from .store import file_lock


class EditorError(ValueError):
    def __init__(self, code: str, report: str = "") -> None:
        super().__init__(code)
        self.code, self.report = code, report


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
) -> None:
    config, errors = load_config(text, known, None, today, available_channels=available_channels)
    if config is None or errors:
        raise EditorError("invalid_profile", "\n".join(f"{e.path}: {e.message}" for e in errors))


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
    if profile_id is None:
        profile: dict[str, Any] = {"id": uuid4().hex}
        data["profiles"].append(profile)
    else:
        index = next((i for i, p in enumerate(data["profiles"]) if p["id"] == profile_id), None)
        if index is None:
            raise EditorError("file_changed")
        # YAML aliases must not let this edit change another profile or section.
        profile = copy.deepcopy(data["profiles"][index])
        data["profiles"][index] = profile
    for key in ("name", "days", "home_station", "work_station", "skip_public_holidays"):
        profile[key] = values[key]
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
    if "compensation_reminder" in values:
        if type(values["compensation_reminder"]) is not bool:
            raise EditorError("invalid_profile")
        alerts = copy.deepcopy(profile.get("alerts", {}))
        if values["compensation_reminder"] or "compensation_reminder" in alerts:
            alerts["compensation_reminder"] = values["compensation_reminder"]
            profile["alerts"] = alerts
    if any(f"channel_{category}" in values for category in CATEGORIES):
        alerts = copy.deepcopy(profile.get("alerts", {}))
        channels = copy.deepcopy(alerts.get("channels", {}))
        for category in CATEGORIES:
            key = f"channel_{category}"
            if key not in values:
                continue
            value = values[key]
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
        journey["first_train"] = values[f"{direction}_time"]
        journey["transfers"] = values[f"{direction}_transfers"]
        buffer = values[f"{direction}_buffer"]
        if isinstance(buffer, float) and buffer.is_integer():
            buffer = int(buffer)
        journey["buffer_min"] = buffer
        weekday_key = f"{direction}_weekday_times"
        if weekday_key in values:
            overrides = values[weekday_key]
            if not isinstance(overrides, dict):
                raise EditorError("invalid_profile")
            if overrides:
                journey["first_train_by_weekday"] = copy.deepcopy(overrides)
            else:
                journey.pop("first_train_by_weekday", None)
        for station in journey["transfers"]:
            minimum.setdefault(station, FALLBACK_MIN)
    minimum.update(values.get("transfer_min", {}))
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
    validate_text(text, known, today, available_channels)
    return text


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

"""Apply household schedule inputs to the runtime's existing profile file."""

import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml


def update_schedule(
    path: Path,
    profile_id: str,
    extra_dates: tuple[date, ...],
    outbound_buffer_min: int,
    return_buffer_min: int,
    *,
    validate: Callable[[str], bool],
) -> str:
    if len(extra_dates) > 366 or not all(
        0 <= minutes <= 120 for minutes in (outbound_buffer_min, return_buffer_min)
    ):
        raise ValueError("Invalid office dates or station buffers.")
    if path.is_symlink() or not path.is_file():
        raise ValueError("The configured profile path must be a regular file.")
    original = path.read_text(encoding="utf-8")
    if not validate(original):
        raise ValueError(
            "The current profile file has errors. Review it before updating the schedule."
        )
    data = yaml.safe_load(original)
    profile = next((entry for entry in data["profiles"] if entry.get("id") == profile_id), None)
    if profile is None:
        raise ValueError("The regular commute profile was not found.")
    dates = {day.isoformat() for day in extra_dates}
    current = {str(day) for day in profile.get("extra_dates", [])}
    if (
        dates == current
        and profile["outbound"].get("buffer_min") == outbound_buffer_min
        and profile["return"].get("buffer_min") == return_buffer_min
    ):
        return original
    profile["extra_dates"] = sorted(dates)
    profile["outbound"] = {**profile["outbound"], "buffer_min": outbound_buffer_min}
    profile["return"] = {**profile["return"], "buffer_min": return_buffer_min}
    updated = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if not validate(updated):
        raise ValueError("The schedule update would make the profile file invalid.")
    temp: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as file:
            temp = Path(file.name)
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())
        if path.is_symlink() or path.read_text(encoding="utf-8") != original:
            raise ValueError("The profile file changed during this update. Try again.")
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return updated

"""Save one selected journey as a tentative calendar plan, without recording attendance.

Serialization follows RFC 5545 sections 3.1, 3.3.11 and 3.6.1:
https://www.rfc-editor.org/rfc/rfc5545.txt
"""

import hashlib
import json
from datetime import UTC, datetime

from train_commute_companion.model import LegKind, LegState, Observation, OccurrenceKey, present
from train_commute_companion.view import OccurrenceView

PLANNER_URL = "https://www.ns.nl/reisplanner/"


class CalendarExportError(ValueError):
    """The selected journey lacks a coherent, usable plan."""


def export_id(key: OccurrenceKey) -> str:
    value = json.dumps([key.profile_id, key.direction.value, key.service_date.isoformat()])
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CalendarExportError("planned time has no timezone")
    return value.astimezone(UTC)


def planned_bounds(occurrence: OccurrenceView) -> tuple[datetime, datetime]:
    """Only the main selected legs qualify. Retained journeys and alternatives never qualify."""
    if occurrence.not_travelling or occurrence.resolved or not occurrence.legs:
        raise CalendarExportError("no active selected journey")
    if occurrence.selected_recovery is not None:
        raise CalendarExportError("the original train plan has a recovery selection")
    previous = None
    for leg in occurrence.legs:
        if leg.state in (
            LegKind.CANCELLED,
            LegKind.CUT_SHORT,
            Observation.MISSING,
            Observation.UNCERTAIN,
        ):
            raise CalendarExportError("selected journey is cancelled or uncertain")
        if leg.progress in (LegState.CANCELLED, LegState.MISSED, LegState.RELOCKED):
            raise CalendarExportError("selected journey is no longer usable")
        if not leg.board.strip() or not leg.alight.strip() or leg.planned_arrival is None:
            raise CalendarExportError("selected journey plan is incomplete")
        departure, arrival = _utc(leg.planned_departure), _utc(leg.planned_arrival)
        if arrival <= departure:
            raise CalendarExportError("planned times are reversed")
        if previous is not None and (
            previous.alight != leg.board or departure < _utc(present(previous.planned_arrival))
        ):
            raise CalendarExportError("planned transfer is disconnected or reversed")
        previous = leg
    return _utc(occurrence.legs[0].planned_departure), _utc(
        present(occurrence.legs[-1].planned_arrival)
    )


def _date(value: datetime) -> str:
    value = _utc(value)
    return f"{value.year:04}{value.month:02}{value.day:02}T{value.hour:02}{value.minute:02}{value.second:02}Z"


def _text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        char for char in normalized if ord(char) >= 32 and ord(char) != 127 or char in "\n\t"
    )
    return (
        normalized.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> bytes:
    lines, current, size = [], "", 0
    for character in line:
        width = len(character.encode("utf-8"))
        if size + width > 75:
            lines.append(current)
            current, size = " ", 1
        current += character
        size += width
    lines.append(current)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def export_ics(occurrence: OccurrenceView, *, exported_at: datetime) -> bytes:
    """One deliberate download creates one tentative event using planned times only."""
    departure, arrival = planned_bounds(occurrence)
    check = occurrence.health.last_ok_at
    checked = check.isoformat(timespec="seconds") if check is not None else "unknown"
    route = "\n".join(
        f"{leg.planned_departure.isoformat(timespec='minutes')} {leg.board} to {leg.alight}"
        for leg in occurrence.legs
    )
    description = (
        f"Saved journey plan. Data: NS. NS source check: {checked}.\n"
        "This saved plan is not live and does not update. It does not prove attendance, boarding or actual arrival.\n"
        + route
    )
    lines = (
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Train Commute Companion//Journey Plan//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{export_id(occurrence.key)}@train-commute-companion.invalid",
        "DTSTAMP:" + _date(exported_at),
        "DTSTART:" + _date(departure),
        "DTEND:" + _date(arrival),
        "STATUS:TENTATIVE",
        "TRANSP:TRANSPARENT",
        "SUMMARY:"
        + _text(f"Saved journey plan: {occurrence.profile_name} {occurrence.key.direction.value}"),
        "LOCATION:" + _text(f"{occurrence.legs[0].board} to {occurrence.legs[-1].alight}"),
        "DESCRIPTION:" + _text(description),
        "URL:" + PLANNER_URL,
        "END:VEVENT",
        "END:VCALENDAR",
    )
    return b"".join(_fold(line) for line in lines)

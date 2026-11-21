"""Show planned journeys in a calendar without recording attendance.

Serialization follows RFC 5545 sections 3.1, 3.3.11 and 3.6.1:
https://www.rfc-editor.org/rfc/rfc5545.txt
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from train_commute_companion.calendar import TZ, resolve_local
from train_commute_companion.lookahead_probe import service_date
from train_commute_companion.model import (
    DatedSchedule,
    Direction,
    LegKind,
    LegState,
    Observation,
    OccurrenceKey,
    Profile,
    Signature,
    direction_key,
    okey,
    present,
)
from train_commute_companion.revisions import find_profile, signature_for
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.store import PersistedState
from train_commute_companion.travel_plans import plan_suppresses
from train_commute_companion.view import OccurrenceView, works_ahead

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


def calendar_feed(st: PersistedState, now: datetime) -> bytes:
    """Fourteen local service dates from the saved plan, with no provider requests."""
    today = now.astimezone(TZ).date()
    end = today + timedelta(days=14)
    instant = _utc(now)
    profiles = () if st.config is None else st.config.profiles
    travel = tuple(st.travel_plans.values())
    works = {
        key: {w.service_date for w in works_ahead(ledger, today)}
        for key, ledger in st.lookahead.items()
    }
    rows: list[tuple[Profile, DatedSchedule, Signature | None]] = []
    for offset in range(-1, 14):  # yesterday's next-day return can run today
        day = today + timedelta(days=offset)
        for profile in profiles:
            if (
                not profile.alerts.calendar_feed
                or profile.home_station == profile.work_station
                or not profile.commutes_on(day)
            ):
                continue
            for direction in Direction:
                sd = service_date(profile, direction, day)
                signature = signature_for(st, profile, direction, sd)
                first = None if signature is None else signature.legs[0].departure
                schedule = dated_schedule(profile, direction, day, first)
                if not plan_suppresses(schedule, profile, travel):
                    rows.append((profile, schedule, signature))
        for plan in travel:
            source = (
                plan.profile if plan.inherits is None else find_profile(st.config, plan.inherits)
            )
            if source is not None and source.alerts.calendar_feed:
                rows.extend(
                    (plan.profile, schedule, None)
                    for schedule in plan.schedules(day)
                    if not plan_suppresses(schedule, plan.profile, travel)
                )

    events: list[str] = []
    for profile, schedule, signature in sorted(rows, key=lambda row: _utc(row[1].first_departure)):
        key = schedule.key
        active = st.occurrences.get(okey(key))
        if (
            not today <= key.service_date < end
            or _utc(schedule.last_departure or schedule.first_departure) < instant
            or profile.paused_on(key.service_date)
            or (active is not None and active.not_travelling)
        ):
            continue
        route = f"{profile.origin(key.direction)} to {profile.destination(key.direction)}"
        start = schedule.first_departure
        end_time = schedule.last_departure
        label = "Departure window" if schedule.mode == "window" else "Planned departure"
        if signature is not None and schedule.mode == "fixed":
            last = signature.legs[-1]
            last_day = key.service_date + timedelta(days=last.day_offset)
            end_time = _utc(resolve_local(last_day, last.departure, edge="start")) + timedelta(
                minutes=last.duration_min
            )
        description = f"{label}. Check NS before you travel."
        if key.service_date in works.get(direction_key(key.profile_id, key.direction), ()):
            description += "\nNS timetable warning recorded for this service date."
        events.extend(
            (
                "BEGIN:VEVENT",
                f"UID:{export_id(key)}@train-commute-companion.invalid",
                "DTSTAMP:" + _date(now),
                "DTSTART:" + _date(start),
            )
        )
        if end_time is not None and _utc(end_time) > _utc(start):
            events.append("DTEND:" + _date(end_time))
        events.extend(
            (
                "STATUS:TENTATIVE",
                "TRANSP:TRANSPARENT",
                "SUMMARY:" + _text(f"{profile.name}: {route}"),
                "DESCRIPTION:" + _text(description),
                "URL:" + PLANNER_URL,
                "END:VEVENT",
            )
        )
    if not events:  # RFC 5545 requires a component even when no trips remain.
        events.extend(("BEGIN:X-TCC-EMPTY", "COMMENT:No planned journeys", "END:X-TCC-EMPTY"))
    lines = (
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Train Commute Companion//Commute Calendar//EN",
        "CALSCALE:GREGORIAN",
        *events,
        "END:VCALENDAR",
    )
    return b"".join(_fold(line) for line in lines)

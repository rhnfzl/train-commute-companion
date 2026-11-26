"""The local daily slots and the short, dated commute summary."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from train_commute_companion.calendar import TZ, hhmm, resolve_local, to_utc
from train_commute_companion.catalogue import MessageLanguage, message, message_date
from train_commute_companion.disruptions import for_connection
from train_commute_companion.lookahead_probe import (
    WORKS_MARKS,
    ProbeResult,
    is_warning,
    latest_mark,
    service_date,
    travels_on,
    works_titles,
)
from train_commute_companion.lookahead_run import directions_due, skipped_dates
from train_commute_companion.messages import crowd_text, leave_by
from train_commute_companion.model import (
    AlertEvent,
    BriefRow,
    DatedSchedule,
    Direction,
    EventKind,
    LegKind,
    Obligation,
    Profile,
    Provenance,
    Severity,
    Stations,
    TransferState,
    direction_key,
    display_name,
)
from train_commute_companion.notification_policy import enabled
from train_commute_companion.providers.base import ActiveDisruptions, PlannedWorks
from train_commute_companion.providers.weather import WeatherView, weather_alert_lines
from train_commute_companion.revisions import (
    find_profile,
    route_geometry_same,
    signature_for,
    travel_replaces,
)
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.store import PersistedState
from train_commute_companion.view import OccurrenceView
from train_commute_companion.weather_allowance import weather_allowance

ONE_DAY = timedelta(days=1)
EVENING_RETRY = (timedelta(minutes=15), timedelta(minutes=30))
LEGACY_FORECAST_CREDIT = " (Open-Meteo https://open-meteo.com/, CC BY 4.0)"


@dataclass(frozen=True, slots=True)
class Due:
    profile: Profile
    horizon: tuple[Direction, ...]
    morning: tuple[Direction, ...]
    evening: tuple[Direction, ...]


def slots(p: Profile, day: date, fallback: time) -> tuple[datetime, datetime]:
    morning = p.brief.at if p.alerts.daily_brief else None
    return (
        resolve_local(day, morning or fallback, edge="start"),
        resolve_local(day, p.brief.evening_at, edge="start"),
    )


def directions_on(st: PersistedState, p: Profile, day: date) -> tuple[Direction, ...]:
    return tuple(
        d
        for d in Direction
        if not travel_replaces(st, p, d, day) and travels_on(p, d, day, skipped_dates(st, p, d))
    )


def open_directions(
    st: PersistedState, p: Profile, day: date, now: datetime
) -> tuple[Direction, ...]:
    if now.astimezone(TZ).date() != day:
        return ()
    return tuple(
        d
        for d in directions_on(st, p, day)
        if to_utc(schedule(st, p, d, day).window_end) > to_utc(now)
    )


def due(st: PersistedState, now: datetime, fallback: time) -> tuple[Due, ...]:
    day = now.astimezone(TZ).date()
    batch: list[Due] = []
    for p in st.config.profiles if st.config else ():
        if p.home_station == p.work_station:
            continue
        morning, evening = slots(p, day, fallback)
        horizon: tuple[Direction, ...] = ()
        brief: tuple[Direction, ...] = ()
        check: tuple[Direction, ...] = ()
        if to_utc(now) >= to_utc(morning):
            horizon = tuple(d for _, d in directions_due(st, morning, profile_id=p.id))
            if enabled(p.alerts, EventKind.DAILY_BRIEF) and st.brief_days.get(p.id) != day:
                brief = open_directions(st, p, day, now)
        if (
            p.alerts.daily_brief
            and enabled(p.alerts, EventKind.WORKS_AHEAD)
            and to_utc(now) >= to_utc(evening)
        ):
            check = tuple(
                d
                for d in directions_on(st, p, day + ONE_DAY)
                if p.journey_on(d, day + ONE_DAY).mode != "window"
                and evening_due(st, p, d, day, now)
            )
        if horizon or brief or check:
            batch.append(Due(p, horizon, brief, check))
    return tuple(batch)


def evening_due(st: PersistedState, p: Profile, d: Direction, day: date, now: datetime) -> bool:
    ledger = st.lookahead.get(direction_key(p.id, d))
    if ledger is None:
        return True
    if ledger.last_evening is not None and ledger.last_evening.astimezone(TZ).date() == day:
        return False
    attempted = ledger.last_evening_attempt
    if attempted is None or attempted.astimezone(TZ).date() != day:
        return True
    failed = ledger.evening_failures
    return 0 < failed <= len(EVENING_RETRY) and (
        to_utc(now) - to_utc(attempted) >= EVENING_RETRY[failed - 1]
    )


def itinerary_changed(old: Profile, new: Profile, d: Direction, day: date) -> bool:
    before, after = old.journey_on(d, day), new.journey_on(d, day)
    return (
        not route_geometry_same(old, new, d)
        or (before.first_train, before.mode) != (after.first_train, after.mode)
        or travels_on(old, d, day, ()) != travels_on(new, d, day, ())
        or old.accessibility.require_accessible != new.accessibility.require_accessible
        or any(old.transfer_min.get(s) != new.transfer_min.get(s) for s in after.transfers)
    )


def has_works_warning(st: PersistedState, p: Profile, d: Direction, sd: date) -> bool:
    ledger = st.lookahead.get(direction_key(p.id, d))
    return ledger is not None and any(
        k.startswith(f"{sd.isoformat()}|") and is_warning(latest_mark(marks, WORKS_MARKS)[0])
        for k, marks in ledger.per_date.items()
    )


def _live_warning(observed: OccurrenceView | None, language: MessageLanguage) -> str | None:
    if observed is None or not observed.health.fresh:
        return None
    for leg in observed.legs:
        if leg.state in (LegKind.CANCELLED, LegKind.CUT_SHORT):
            train = message(
                "brief.train", language, time=hhmm(leg.planned_departure), number=leg.train_number
            )
            status = message(f"brief.{leg.state.value}", language)
            return f"{train}: {status}"
    for transfer in observed.transfers:
        if transfer.state is TransferState.BROKEN:
            return message("brief.connection", language, station=transfer.station)
    if observed.arrive_by is not None and observed.arrival_target_status in ("late", "no_train"):
        return message("brief.arrive_by", language, time=hhmm(observed.arrive_by))
    return None


def _train_row(
    p: Profile,
    d: Direction,
    day: date,
    planned: datetime,
    observed: OccurrenceView | None,
    probe: ProbeResult | None,
    weather: WeatherView | None,
    now: datetime,
) -> BriefRow:
    j = p.journey_on(d, day)
    language = p.alerts.language
    sd = service_date(p, d, day)
    buffer = j.buffer_min if p.alerts.leave_by else None
    crowd_enabled = enabled(p.alerts, EventKind.CROWD_CHANGE)
    if observed is not None and observed.legs and observed.health.fresh:
        first = observed.legs[0]
        planned = first.planned_departure
        departure = first.departure or planned
        if warning := _live_warning(observed, language):
            return BriefRow(d, sd, warning, True)
        if (
            first.state in (LegKind.OK, LegKind.DELAYED)
            and not observed.route_changed
            and not (probe and (probe.reasons or probe.unclear))
        ):
            text = message("brief.train", language, time=hhmm(departure), number=first.train_number)
            crowd = first.crowd_forecast if crowd_enabled else None
            if crowd == "high":
                text += message("leg.crowd", language, level=crowd)
            actionable = crowd == "high" or (first.departure_delay_min or 0) >= p.alerts.delay_min
            advice = observed.format_leave_by(language) if buffer is not None else None
            return BriefRow(
                d,
                sd,
                text,
                actionable,
                observed.leave_by,
                advice,
                advice_actionable=observed.weather_allowance is not None,
            )
    if probe is not None and probe.connection is not None and not probe.unclear:
        legs = probe.connection.legs
        first_leg = legs[0]
        scheduled = first_leg.train.planned_departure
        obs = first_leg.board.departure
        expected = obs.value if obs is not None and obs.provenance is Provenance.REPORTED else None
        departure = expected or scheduled
        text = message(
            "brief.train", language, time=hhmm(departure), number=first_leg.train.train_number
        )
        if probe.reasons:
            for reason in probe.reasons:
                if reason.tag in ("cancelled", "cut_short") and reason.leg is not None:
                    train = legs[reason.leg].train
                    label = message(
                        "brief.train",
                        language,
                        time=hhmm(train.planned_departure),
                        number=train.train_number,
                    )
                    status = message(f"brief.{reason.tag}", language)
                    return BriefRow(d, sd, f"{label}: {status}", True)
                if reason.tag == "short":
                    return BriefRow(
                        d,
                        sd,
                        message("brief.connection", language, station=reason.stop or "?"),
                        True,
                    )
            return BriefRow(d, sd, message("brief.listed", language, train=text), True)
        crowded = crowd_enabled and any(x.crowd_forecast == "high" for x in legs)
        if crowded:
            text += crowd_text(legs, language)
        base = None if buffer is None else leave_by(scheduled, expected, buffer)
        allowance = (
            weather_allowance(base, buffer, weather, now)
            if base is not None
            and buffer is not None
            and weather is not None
            and p.alerts.weather_allowance
            else None
        )
        moved = abs(to_utc(departure) - to_utc(planned)) >= timedelta(minutes=p.alerts.delay_min)
        return BriefRow(
            d,
            sd,
            text,
            moved or crowded,
            allowance.suggested_leave_by if allowance else base,
            allowance.format(language)
            if allowance
            else message("leave_by", language, time=hhmm(base))
            if base
            else None,
            advice_actionable=allowance is not None,
        )
    if probe is not None and (probe.reasons or probe.unclear):
        return BriefRow(d, sd, message("brief.scheduled", language, time=hhmm(planned)), True)
    if j.mode == "window":
        window = observed.window if observed is not None else None
        end = hhmm(window[1]) if window is not None else f"{j.window[1]:%H:%M}"
        return BriefRow(d, sd, message("brief.window", language, start=hhmm(planned), end=end))
    return BriefRow(d, sd, message("brief.details_unavailable", language, time=hhmm(planned)))


def build_row(
    p: Profile,
    d: Direction,
    day: date,
    planned: datetime,
    observed: OccurrenceView | None,
    results: Sequence[ProbeResult],
    prior_warning: bool,
    works: PlannedWorks,
    weather: WeatherView | None,
    disruptions: ActiveDisruptions | None,
    now: datetime,
    stations: Stations | None = None,
) -> BriefRow:
    whole = next((r for r in results if r.leg_index is None), None)
    row = _train_row(p, d, day, planned, observed, whole, weather, now)
    language = p.alerts.language
    notes = []
    actionable = row.actionable
    if enabled(p.alerts, EventKind.WORKS_AHEAD):
        if titles := works_titles(results, works):
            notes.append(message("brief.works", language, titles=", ".join(titles)))
            actionable = True
        if prior_warning and not (whole and whole.reasons):
            if not titles:
                notes.append(message("brief.works_changed", language))
            actionable = True
            row = replace(row, leave_by=None, advice=None)
        elif not titles and works.coverage != "complete":
            notes.append(message("brief.works_unknown", language))
    if enabled(p.alerts, EventKind.DISRUPTION):
        incidents = _incidents(observed, whole, disruptions, planned, now)
        notes.extend(message("brief.disruption", language, title=title) for title in incidents)
        actionable |= bool(incidents)
    if weather is not None:
        notes.extend(weather_alert_lines((weather,), language, stations))
        actionable |= bool(weather.warnings)
    return replace(row, notes=tuple(notes), actionable=actionable)


def _incidents(
    observed: OccurrenceView | None,
    probe: ProbeResult | None,
    feed: ActiveDisruptions | None,
    planned: datetime,
    now: datetime,
) -> tuple[str, ...]:
    if observed is not None and observed.disruptions_coverage == "complete":
        departure = observed.legs[0].departure if observed.legs else None
        starts = max(to_utc(now), to_utc(departure or planned))
        return tuple(
            item.title
            for item in observed.disruptions
            if item.end is None or to_utc(item.end) > starts
        )
    if feed is not None and probe is not None and probe.connection is not None:
        return tuple(item.title for item in for_connection(feed, probe.connection, now))
    return ()


def _render(
    p: Profile, day: date, now: datetime, rows: Sequence[BriefRow], language: MessageLanguage
) -> str | None:
    if not rows:
        return None

    def label_date(d: date) -> str:
        return f"{d:%a %d %b}" if language == "en" else message_date(d, language)

    lines = [message("brief.heading", language, name=display_name(p.name), date=label_date(day))]
    disruption_prefix = message("brief.disruption", language, title="")
    notes_by_row = [
        tuple(
            dict.fromkeys(
                note.removesuffix(" (CC BY 4.0)").rstrip(".")
                for note in row.notes
                if not note.endswith(LEGACY_FORECAST_CREDIT)
            )
        )
        for row in rows
    ]
    incidents = [
        {note for note in notes if note.startswith(disruption_prefix)} for notes in notes_by_row
    ]
    shared = set.intersection(*incidents) if len(incidents) > 1 else set()
    actionable = False
    for row, notes in zip(rows, notes_by_row, strict=True):
        label = message(f"direction.{row.direction.value}", language).title()
        if row.service_date != day:
            label += f" ({label_date(row.service_date)})"
        advice = (
            (row.advice,)
            if (
                row.advice is not None
                and row.leave_by is not None
                and to_utc(row.leave_by) > to_utc(now)
            )
            else ()
        )
        actionable |= row.actionable or (bool(advice) and row.advice_actionable)
        local_notes = (note for note in notes if note not in shared)
        lines.append("\n".join((f"{label}: {row.details.rstrip('.')}", *advice, *local_notes)))
    if shared:
        lines.append("\n".join(note for note in notes_by_row[0] if note in shared))
    return "\n\n".join(lines) if actionable or not p.brief.only_if_something else None


def obligation(
    p: Profile, revision: int, day: date, now: datetime, rows: Sequence[BriefRow]
) -> Obligation | None:
    text = _render(p, day, now, rows, p.alerts.language)
    if text is None:
        return None
    key = f"daily_brief|{p.id}|{day.isoformat()}"
    event = AlertEvent(
        p.id,
        p.name,
        None,
        day,
        None,
        EventKind.DAILY_BRIEF,
        Severity.SILENT,
        None,
        text,
        key,
        now,
    )
    return Obligation(
        key,
        event,
        None,
        None,
        revision,
        None,
        key,
        resolve_local(day + ONE_DAY, time.min, edge="start"),
        0,
        brief_rows=tuple(rows),
    )


def current_text(
    st: PersistedState,
    o: Obligation,
    now: datetime,
    observations: Mapping[Direction, OccurrenceView] | None = None,
) -> str | None:
    p = find_profile(st.config, o.event.profile_id)
    then = find_profile(st.config_snapshots.get(o.route_revision), o.event.profile_id)
    day = o.event.service_date
    if (
        p is None
        or then is None
        or replace(
            p,
            alerts=replace(
                p.alerts,
                channels=then.alerts.channels,
                ntfy_enabled=then.alerts.ntfy_enabled,
                live_today=then.alerts.live_today,
                telegram_status_pin=then.alerts.telegram_status_pin,
                quick_trip=then.alerts.quick_trip,
                navigate=then.alerts.navigate,
                timetable_update=then.alerts.timetable_update,
                arrival_prompt=then.alerts.arrival_prompt,
                compensation_details=then.alerts.compensation_details,
                wake_at=then.alerts.wake_at,
                checked_alternatives=then.alerts.checked_alternatives,
                partner_eta=then.alerts.partner_eta,
                partner_phone=then.alerts.partner_phone,
                language=then.alerts.language,
            ),
        )
        != then
        or day is None
    ):
        return None
    eligible = open_directions(st, p, day, now)
    if o.brief_rows:
        language = then.alerts.language
        rows = []
        for row in o.brief_rows:
            if row.direction not in eligible:
                continue
            observation = observations.get(row.direction) if observations is not None else None
            if warning := _live_warning(observation, language):
                row = replace(
                    row,
                    details=warning,
                    actionable=True,
                    leave_by=None,
                    advice=None,
                    advice_actionable=False,
                )
            rows.append(row)
        return _render(p, day, now, rows, language)
    if p.brief.only_if_something:
        return None
    # Older saved briefs lack advice timestamps. Keep the remaining rows and omit advice.
    header, _, body = o.event.text.partition("\n")
    kept = [
        re.sub(
            r"\. Leave by \d{2}:\d{2}(?: \(\+\d+ min suggested for forecast [^)]+\))?",
            "",
            line,
            count=1,
        )
        for line in body.splitlines()
        if any(
            line.startswith((f"{d.value.title()}:", f"{d.value.title()} ("))
            for d in o.brief_directions
            if d in eligible
        )
    ]
    return "\n".join((header, *kept)) if kept else None


def schedule(st: PersistedState, p: Profile, d: Direction, day: date) -> DatedSchedule:
    signature = signature_for(st, p, d, service_date(p, d, day))
    first = None if signature is None else signature.legs[0].departure
    return dated_schedule(p, d, day, first)

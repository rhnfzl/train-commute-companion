"""A weekly summary states what NS checks cover, including dates with unknown evidence."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum

from train_commute_companion.calendar import TZ, resolve_local, to_utc
from train_commute_companion.catalogue import MessageLanguage, message, message_date
from train_commute_companion.lookahead_probe import (
    ProbeResult,
    evaluate,
    probe_queries,
    service_date,
    works_titles,
)
from train_commute_companion.model import (
    AlertEvent,
    Direction,
    EventKind,
    Obligation,
    Profile,
    Severity,
    direction_key,
    display_name,
)
from train_commute_companion.notification_policy import enabled
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.revisions import baseline_on
from train_commute_companion.serde import encode
from train_commute_companion.state import BaselineHistory, PollFailure
from train_commute_companion.store import PersistedState

TIME_LIMIT_S = 90.0
RETRY_AFTER = timedelta(minutes=2)


def profile_token(profile: Profile) -> str:
    # Channel/category edits use live delivery policy; they must not add recipients to old facts.
    route = replace(
        profile, alerts=replace(profile.alerts, channels={}, disabled_categories=frozenset())
    )
    data = encode(route)
    for key in (
        "commute_stats",
        "weekly_recap",
        "after_trip_note",
        "alert_history",
        "calendar_feed",
        "location_leave_now",
        "checked_alternatives",
        "partner_eta",
        "partner_phone",
        "language",
    ):
        data["alerts"].pop(key)
    if profile.repeat is None:
        data.pop("repeat")
    # Pending weekly digests keep the same identity when daily brief settings are added or edited.
    data.pop("brief")
    data["alerts"].pop("daily_brief")
    data["alerts"].pop("quick_trip")
    for key in ("arrival_prompt", "navigate", "compensation_details", "timetable_update"):
        data["alerts"].pop(key)
    data["alerts"].pop("wake_at")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class DigestPhase(StrEnum):
    DISABLED = "disabled"
    WAITING_SLOT = "waiting_slot"
    QUEUED = "queued"
    COOLDOWN = "cooldown"
    DUE = "due"


@dataclass(frozen=True, slots=True)
class DigestDecision:
    phase: DigestPhase
    week: date | None = None


def digest_decision(
    profile: Profile,
    now: datetime,
    last_week: date | None,
    attempted_at: datetime | None = None,
) -> DigestDecision:
    setting = profile.works_digest
    if setting is None or not enabled(profile.alerts, EventKind.WORKS_DIGEST):
        return DigestDecision(DigestPhase.DISABLED)
    local = now.astimezone(TZ)
    if local.isoweekday() != setting.day:
        return DigestDecision(DigestPhase.WAITING_SLOT)
    slot = resolve_local(local.date(), setting.time, edge="start")
    week = local.date() - timedelta(days=local.weekday())
    if last_week is not None and week <= last_week:
        return DigestDecision(DigestPhase.QUEUED, week)
    if to_utc(now) < to_utc(slot):
        return DigestDecision(DigestPhase.WAITING_SLOT, week)
    if attempted_at is not None and timedelta() <= to_utc(now) - to_utc(attempted_at) < RETRY_AFTER:
        return DigestDecision(DigestPhase.COOLDOWN, week)
    return DigestDecision(DigestPhase.DUE, week)


def due_week(profile: Profile, now: datetime, last_week: date | None) -> date | None:
    decision = digest_decision(profile, now, last_week)
    return decision.week if decision.phase is DigestPhase.DUE else None


@dataclass(frozen=True, slots=True)
class DigestRow:
    commute_date: date
    direction: Direction
    service_date: date
    status: str
    notes: tuple[str, ...] = ()


def _row(
    day: date,
    direction: Direction,
    sd: date,
    results: Sequence[ProbeResult],
    works: PlannedWorks,
    language: MessageLanguage,
) -> DigestRow:
    titles = works_titles(results, works)
    notes = tuple(message("digest.works", language, title=title) for title in titles)
    whole = next((result for result in results if result.leg_index is None), None)
    if whole is None or whole.connection is None or any(r.unclear for r in results):
        return DigestRow(day, direction, sd, message("digest.route_unknown", language), notes)
    if titles:
        return DigestRow(day, direction, sd, message("digest.affected", language), notes)
    if works.coverage == "complete" and all(result.clean for result in results):
        return DigestRow(day, direction, sd, message("digest.clear", language))
    effects = sorted({reason.tag for result in results for reason in result.reasons})
    if effects:
        labels = ", ".join(message(f"digest.reason.{effect}", language) for effect in effects)
        notes += (message("digest.effects", language, effects=labels),)
    return DigestRow(day, direction, sd, message("digest.coverage_unknown", language), notes)


async def check_week(
    state: PersistedState,
    profile: Profile,
    start: datetime,
    works: Callable[[], Awaitable[PlannedWorks | None]],
    fetch: Callable[[Sequence[Query]], Awaitable[tuple[ProviderAnswer, ...] | PollFailure]],
    now: Callable[[], datetime],
) -> tuple[DigestRow, ...]:
    """At most fourteen date/direction probes, with one total network time limit."""
    today = start.astimezone(TZ).date()
    language = profile.alerts.language
    rows: dict[tuple[date, Direction], DigestRow] = {}
    pending = []
    for offset in range(1, 8):
        day = today + timedelta(days=offset)
        commute = profile.commutes_on(day)
        for direction in Direction:
            sd = service_date(profile, direction, day)
            status = message("digest.incomplete" if commute else "digest.not_scheduled", language)
            if commute and profile.paused_on(sd):
                status = message("digest.paused", language)
            rows[day, direction] = DigestRow(day, direction, sd, status)
            if commute and not profile.paused_on(sd):
                pending.append((day, direction, sd))
    if not pending:
        return tuple(rows.values())
    try:
        async with asyncio.timeout(TIME_LIMIT_S):
            listed = await works()
            coverage = listed or PlannedWorks((), "unknown")
            for day, direction, sd in pending:
                history = state.baselines.get(
                    direction_key(profile.id, direction), BaselineHistory()
                )
                baseline = baseline_on(history, profile, direction, sd)
                queries = probe_queries(profile, direction, day, baseline)
                answer = await fetch(queries)
                if isinstance(answer, str):
                    rows[day, direction] = DigestRow(
                        day, direction, sd, message("digest.request_failed", language)
                    )
                    continue
                if (
                    len(answer) != len(queries)
                    or {a.query for a in answer} != set(queries)
                    or any(
                        a.fetched_at.tzinfo is None
                        or a.fetched_at.utcoffset() is None
                        or not to_utc(start) <= to_utc(a.fetched_at) <= to_utc(now())
                        for a in answer
                    )
                ):
                    rows[day, direction] = DigestRow(
                        day, direction, sd, message("digest.invalid", language)
                    )
                    continue
                results = evaluate(profile, direction, day, answer, coverage, baseline)
                rows[day, direction] = _row(day, direction, sd, results, coverage, language)
    except TimeoutError:
        pass  # Remaining dates keep their explicit unknown labels.
    return tuple(rows.values())


def obligation(
    profile: Profile, revision: int, week: date, start: datetime, rows: Sequence[DigestRow]
) -> Obligation:
    day = start.astimezone(TZ).date()
    language = profile.alerts.language
    first, last = day + timedelta(days=1), day + timedelta(days=7)
    text = [
        message(
            "digest.title",
            language,
            name=display_name(profile.name),
            first=f"{first:%d %b}" if language == "en" else message_date(first, language),
            last=f"{last:%d %b}" if language == "en" else message_date(last, language),
        ),
        message(
            "digest.started", language, time=start.astimezone(TZ).isoformat(timespec="minutes")
        ),
        message("digest.dates", language),
    ]
    for row in rows:
        suffix = (
            ""
            if row.service_date == row.commute_date
            else message("digest.service_date", language, date=row.service_date.isoformat())
        )
        row_day = (
            f"{row.commute_date:%a %d %b}"
            if language == "en"
            else message_date(row.commute_date, language)
        )
        direction = message(f"direction.{row.direction.value}", language)
        text.append(f"{row_day} {direction}{suffix}: {row.status}.")
        text.extend("  " + note for note in row.notes)
    text.append(message("digest.check_again", language))
    key = f"works_digest|{profile.id}|{week.isoformat()}"
    event = AlertEvent(
        profile.id,
        profile.name,
        None,
        None,
        None,
        EventKind.WORKS_DIGEST,
        Severity.SILENT,
        None,
        "\n".join(text),
        key,
        start,
    )
    return Obligation(
        key,
        event,
        None,
        None,
        revision,
        None,
        key,
        resolve_local(day + timedelta(days=1), datetime.min.time(), edge="start"),
        0,
        digest_token=profile_token(profile),
    )

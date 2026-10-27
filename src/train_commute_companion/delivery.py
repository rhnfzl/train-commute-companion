"""Durable delivery obligations: revalidated right before sending (7.12 rule 5)."""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal, Protocol

from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.model import (
    AlertEvent,
    Direction,
    EventKind,
    OccurrenceKey,
    Severity,
)
from train_commute_companion.notify import Notifier, WebhookPort
from train_commute_companion.webhook import url_id

APP_WARNINGS = frozenset(
    {EventKind.KEY_REJECTED, EventKind.PROFILE_REFUSED, EventKind.STORAGE_FAILED}
)
JOURNEY_WARNINGS = frozenset(
    {
        EventKind.CANCELLED,
        EventKind.CUT_SHORT,
        EventKind.TRANSFER_BROKEN,
        EventKind.MISSING,
        EventKind.ROUTE_CHANGED,
        EventKind.STOPPED_FOLLOWING,
    }
)


@dataclass(frozen=True, slots=True)
class Obligation:
    id: str
    event: AlertEvent
    occurrence: OccurrenceKey | None
    look_ahead: tuple[str, Direction, date] | None
    route_revision: int
    baseline_version: int | None
    fact_key: str
    expires_at: datetime | None
    attempts: int
    webhook_copied: bool = False  # its webhook copy was made (stored before: made next pass)


@dataclass(frozen=True, slots=True)
class WebhookCopy:
    """A durable webhook copy of an alert, apart from its phone obligation: a receiver can
    never change phone delivery. `id` is `webhook.event_id(obligation.id)`; `done` holds the
    `url_id` of every URL that accepted it, never a URL; `last_try` is the latest pass in which
    some URL was really tried."""

    id: str
    obligation: Obligation
    done: tuple[str, ...] = ()
    first_try: datetime | None = None
    last_try: datetime | None = None


class LedgerView(Protocol):
    def skipped(self, key: OccurrenceKey) -> bool: ...
    def fact_holds(self, o: Obligation) -> bool: ...
    def newer_covers(self, o: Obligation) -> bool: ...
    def revision_live(self, key: OccurrenceKey, rev: int) -> bool: ...
    def in_quiet_hours(self, profile_id: str, now: datetime) -> bool: ...
    def current(self, o: Obligation) -> AlertEvent:
        """The message regenerated from the current state right before sending (rule 5): the
        current profile name, a journey warning naming the current lock and its next good
        connection now, never the replacement frozen when the obligation was written."""
        ...


def revalidate(o: Obligation, view: LedgerView, now: datetime) -> Literal["send", "drop", "hold"]:
    kind = o.event.kind
    if kind in APP_WARNINGS:  # (a) beats (c)
        return "send"
    if kind in JOURNEY_WARNINGS:
        return "drop" if o.occurrence is not None and view.skipped(o.occurrence) else "send"
    if o.expires_at is not None and now.astimezone() >= o.expires_at.astimezone():
        return "drop"
    if view.newer_covers(o) or not view.fact_holds(o):
        return "drop"
    if o.occurrence is not None and not view.revision_live(o.occurrence, o.route_revision):
        return "drop"
    if (
        o.event.severity is Severity.SILENT
        and kind is not EventKind.ALL_GOOD
        and view.in_quiet_hours(o.event.profile_id, now)
    ):
        return "hold"
    return "send"


def pending_journey_warnings(obligations: Sequence[Obligation], key: OccurrenceKey) -> int:
    """What callers pass to `lifecycle.ends`: undelivered journey warnings of this occurrence."""
    return sum(1 for o in obligations if o.occurrence == key and o.event.kind in JOURNEY_WARNINGS)


def zone_report_stops_silent(s: OccurrenceState, zones: Collection[str]) -> bool:
    """Rule 6 / 8.4: a report in the alighting-station or destination zone ends repeat silent
    messages for the journey. Use as the silent branch of `LedgerView.fact_holds`."""
    return any(z in s.zone_reports for z in zones)


async def deliver(
    o: Obligation, view: LedgerView, notifier: Notifier, now: datetime
) -> Obligation | None:
    """One watchdog attempt. None: finished (sent or dropped). Otherwise the obligation to keep."""
    verdict = revalidate(o, view, now)
    if verdict == "drop":
        return None
    if verdict == "hold":
        return o
    if await notifier.send(view.current(o)):
        return None
    return replace(o, attempts=o.attempts + 1)


async def post_copy(
    c: WebhookCopy,
    view: LedgerView,
    port: WebhookPort,
    now: datetime,
    started: Callable[[WebhookCopy], None] = lambda c: None,
) -> WebhookCopy | None:
    """One try of a webhook copy, judged as its phone obligation is (rule 5). None: finished
    (every URL accepted, or dropped). Otherwise the copy to keep; the give-up clock starts
    only with a real request, and `started` gets the copy with its first try before that
    request goes out, so a try cut off by shutdown still counts (owner decision Q1)."""
    verdict = revalidate(c.obligation, view, now)
    if verdict == "drop":
        return None
    if verdict == "hold":
        return c
    todo = [u for u in port.urls if url_id(u) not in c.done]
    if not todo:
        return None
    if not port.due(todo):
        return c
    if c.first_try is None:
        c = replace(c, first_try=now)
        started(c)
    tried, ok = await port.post(view.current(c.obligation), c.id, todo)
    if not tried:
        return c
    done = c.done + tuple(url_id(u) for u in todo if u in ok)
    if all(url_id(u) in done for u in port.urls):
        return None
    return replace(c, done=done, last_try=now)

"""Durable delivery obligations: revalidated right before sending (7.12 rule 5)."""

from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Literal, Protocol

from train_commute_companion.calendar import to_utc
from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.model import (
    DELAY_NEWS,
    AlertEvent,
    EventKind,
    Obligation,
    OccurrenceKey,
    Severity,
    WebhookCopy,
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


def covers(x: Obligation, o: Obligation) -> bool:
    """Rule 5(b): `x` is a newer message about what `o` reported (same occurrence and train,
    the same kind or both delay news). Rule 5(a): a warning is never covered."""

    def family(k: EventKind) -> frozenset[EventKind]:
        return DELAY_NEWS if k in DELAY_NEWS else frozenset({k})

    return (
        o.occurrence is not None
        and o.event.kind not in APP_WARNINGS | JOURNEY_WARNINGS
        and x.id != o.id
        and x.occurrence == o.occurrence
        and x.event.kind in family(o.event.kind)
        and x.event.train == o.event.train
        and to_utc(x.event.created_at) > to_utc(o.event.created_at)
    )


class Ledger(Protocol):
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


def revalidate(o: Obligation, ledger: Ledger, now: datetime) -> Literal["send", "drop", "hold"]:
    kind = o.event.kind
    if kind in APP_WARNINGS:  # (a) beats (c)
        return "send"
    if kind in JOURNEY_WARNINGS:
        return "drop" if o.occurrence is not None and ledger.skipped(o.occurrence) else "send"
    if o.expires_at is not None and now.astimezone() >= o.expires_at.astimezone():
        return "drop"
    if ledger.newer_covers(o) or not ledger.fact_holds(o):
        return "drop"
    if o.occurrence is not None and not ledger.revision_live(o.occurrence, o.route_revision):
        return "drop"
    if (
        o.event.severity is Severity.SILENT
        and kind is not EventKind.ALL_GOOD
        and ledger.in_quiet_hours(o.event.profile_id, now)
    ):
        return "hold"
    return "send"


def pending_journey_warnings(obligations: Sequence[Obligation], key: OccurrenceKey) -> int:
    """What callers pass to `lifecycle.ends`: undelivered journey warnings of this occurrence."""
    return sum(1 for o in obligations if o.occurrence == key and o.event.kind in JOURNEY_WARNINGS)


def zone_report_stops_silent(s: OccurrenceState, zones: Collection[str]) -> bool:
    """Rule 6 / 8.4: a report in the alighting-station or destination zone ends repeat silent
    messages for the journey. Use as the silent branch of `Ledger.fact_holds`."""
    return any(z in s.zone_reports for z in zones)


async def deliver(
    o: Obligation, ledger: Ledger, notifier: Notifier, now: datetime
) -> tuple[Obligation | None, bool]:
    """One watchdog attempt: the obligation to keep (None: finished) and whether it was sent."""
    verdict = revalidate(o, ledger, now)
    if verdict == "drop":
        return None, False
    if verdict == "hold":
        return o, False
    if await notifier.send(ledger.current(o)):
        return None, True
    return replace(o, attempts=o.attempts + 1), False


async def post_copy(
    c: WebhookCopy,
    ledger: Ledger,
    port: WebhookPort,
    now: datetime,
    started: Callable[[WebhookCopy], None] = lambda c: None,
) -> WebhookCopy | None:
    """One try of a webhook copy, judged as its phone obligation is (rule 5). None: finished
    (every URL accepted, or dropped). Otherwise the copy to keep; the give-up clock starts
    only with a real request, and `started` gets the copy with its first try before that
    request goes out, so a try cut off by shutdown still counts."""
    verdict = revalidate(c.obligation, ledger, now)
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
    tried, ok = await port.post(ledger.current(c.obligation), c.id, todo)
    if not tried:
        return c
    done = c.done + tuple(url_id(u) for u in todo if u in ok)
    if all(url_id(u) in done for u in port.urls):
        return None
    return replace(c, done=done, last_try=now)

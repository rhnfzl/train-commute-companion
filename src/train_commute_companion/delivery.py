"""Durable delivery obligations: revalidated right before sending (7.12 rule 5)."""

import asyncio
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from train_commute_companion.calendar import to_utc
from train_commute_companion.model import (
    DELAY_NEWS,
    AlertEvent,
    EventKind,
    NotificationChannel,
    Obligation,
    OccurrenceKey,
    Severity,
    WebhookCopy,
    alert_facts,
)
from train_commute_companion.notify import NotificationPaused, Notifier, WebhookPort, destination_id

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


class DeliveryVerdict(StrEnum):
    SEND = "send"
    HOLD = "hold"
    DROP = "drop"


class BindingPhase(StrEnum):
    UNBOUND = "unbound"
    PRIMARY_PENDING = "primary_pending"
    BOUND = "bound"


def binding_phase(obligation: Obligation) -> BindingPhase:
    if obligation.primary_target_pending:
        return BindingPhase.PRIMARY_PENDING
    if obligation.target_ids is None:
        return BindingPhase.UNBOUND
    return BindingPhase.BOUND


class CopyPhase(StrEnum):
    UNBOUND = "unbound"
    QUEUED = "queued"
    STARTED = "started"
    PARTIAL = "partial"
    COMPLETE = "complete"


def copy_phase(copy: WebhookCopy) -> CopyPhase:
    if copy.target_ids is None:
        return CopyPhase.UNBOUND
    if copy.target_ids <= set(copy.done):
        return CopyPhase.COMPLETE
    if copy.done:
        return CopyPhase.PARTIAL
    return CopyPhase.STARTED if copy.first_try is not None else CopyPhase.QUEUED


def start_copy(copy: WebhookCopy, now: datetime) -> WebhookCopy:
    if copy_phase(copy) is CopyPhase.UNBOUND:
        raise ValueError("a copy cannot start before its audience is bound")
    return replace(copy, first_try=copy.first_try or now)


def receive_copy(copy: WebhookCopy, destinations: Collection[str], now: datetime) -> WebhookCopy:
    if copy.first_try is None or copy.target_ids is None:
        raise ValueError("a receipt cannot precede a bound attempt")
    accepted = sorted(set(destinations) & copy.target_ids - set(copy.done))
    return replace(
        copy,
        done=(*copy.done, *accepted),
        last_try=max((copy.first_try, copy.last_try or now, now), key=to_utc),
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
        and alert_facts(x.event).disruption_id == alert_facts(o.event).disruption_id
        and to_utc(x.event.created_at) > to_utc(o.event.created_at)
    )


class Ledger(Protocol):
    def notification_enabled(self, o: Obligation) -> bool: ...
    def channel_enabled(self, o: Obligation, channel: NotificationChannel) -> bool: ...
    def routing_ready(self, o: Obligation) -> bool: ...
    def destination_allowed(self, o: Obligation, destination: str) -> bool: ...
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


def revalidate(o: Obligation, ledger: Ledger, now: datetime) -> DeliveryVerdict:
    kind = o.event.kind
    if kind in APP_WARNINGS:  # (a) beats (c)
        return DeliveryVerdict.SEND
    if not ledger.notification_enabled(o):
        return DeliveryVerdict.DROP
    if not ledger.routing_ready(o):
        return DeliveryVerdict.HOLD
    if kind in JOURNEY_WARNINGS:
        return (
            DeliveryVerdict.DROP
            if o.occurrence is not None and ledger.skipped(o.occurrence)
            else DeliveryVerdict.SEND
        )
    if o.expires_at is not None and now.astimezone() >= o.expires_at.astimezone():
        return DeliveryVerdict.DROP
    if ledger.newer_covers(o) or not ledger.fact_holds(o):
        return DeliveryVerdict.DROP
    if o.occurrence is not None and not ledger.revision_live(o.occurrence, o.route_revision):
        return DeliveryVerdict.DROP
    if (
        o.event.severity is Severity.SILENT
        and kind is not EventKind.ALL_GOOD
        and ledger.in_quiet_hours(o.event.profile_id, now)
    ):
        return DeliveryVerdict.HOLD
    return DeliveryVerdict.SEND


def pending_journey_warnings(obligations: Sequence[Obligation], key: OccurrenceKey) -> int:
    """What callers pass to `lifecycle_end.may_archive`: undelivered journey warnings of this occurrence."""
    return sum(1 for o in obligations if o.occurrence == key and o.event.kind in JOURNEY_WARNINGS)


def primary_delivery_verdict(
    o: Obligation,
    ledger: Ledger,
    now: datetime,
    destination: str | None,
    audience_written: bool,
) -> DeliveryVerdict:
    verdict = revalidate(o, ledger, now)
    if verdict is not DeliveryVerdict.SEND:
        return verdict
    if not ledger.channel_enabled(o, "primary"):
        return DeliveryVerdict.DROP
    if binding_phase(o) is BindingPhase.PRIMARY_PENDING or destination is None:
        return DeliveryVerdict.HOLD
    if o.target_ids is not None and destination not in o.target_ids:
        return DeliveryVerdict.DROP
    if o.event.kind in (EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED) and (
        destination is None or not ledger.destination_allowed(o, destination)
    ):
        return DeliveryVerdict.DROP
    if not audience_written and o.event.kind is not EventKind.STORAGE_FAILED:
        return DeliveryVerdict.HOLD
    return DeliveryVerdict.SEND


async def deliver(
    o: Obligation,
    ledger: Ledger,
    notifier: Notifier,
    now: datetime,
    destination: str | None = "legacy-primary",
    *,
    audience_written: bool = True,
) -> tuple[Obligation | None, bool]:
    """One watchdog attempt: the obligation to keep (None: finished) and whether it was sent."""
    verdict = primary_delivery_verdict(o, ledger, now, destination, audience_written)
    if verdict is DeliveryVerdict.DROP:
        return None, False
    if verdict is DeliveryVerdict.HOLD:
        return o, False
    try:
        if await notifier.send(ledger.current(o)):
            return None, True
    except NotificationPaused:
        return o, False
    return replace(o, attempts=o.attempts + 1), False


def copy_destinations(c: WebhookCopy, port: WebhookPort) -> tuple[str, ...]:
    """Only a pinned original authentication scope can receive a queued copy."""
    return tuple(
        u for u in port.urls if c.target_ids is not None and destination_id(port, u) in c.target_ids
    )


async def post_copy(
    c: WebhookCopy,
    ledger: Ledger,
    port: WebhookPort,
    now: datetime,
    started: Callable[[WebhookCopy], None] = lambda c: None,
    *,
    audience_written: Callable[[WebhookCopy], bool] = lambda c: True,
) -> WebhookCopy | None:
    """Record each destination receipt before waiting for another destination.

    Cancellation retains completed receipts and drains unfinished network tasks. The usual
    crash between remote acceptance and a durable write can still repeat that destination.
    """
    verdict = revalidate(c.obligation, ledger, now)
    if verdict is DeliveryVerdict.DROP:
        return None
    if verdict is DeliveryVerdict.HOLD:
        return c
    destinations = copy_destinations(c, port)
    todo = [
        u
        for u in destinations
        if destination_id(port, u) not in c.done
        and ledger.channel_enabled(c.obligation, port.channels[u])
        and (
            c.obligation.event.kind not in (EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED)
            or ledger.destination_allowed(c.obligation, destination_id(port, u))
        )
    ]
    if not todo:
        return None
    due = port.due(todo)
    if not due:
        return c
    if not audience_written(c):
        return c
    c = start_copy(c, now)
    started(c)
    event = ledger.current(c.obligation)
    pending = {asyncio.create_task(port.post(event, c.id, (u,))) for u in todo if u in due}

    def received(task: asyncio.Task[tuple[frozenset[str], frozenset[str]]]) -> None:
        nonlocal c
        tried, ok = task.result()
        if tried:
            c = receive_copy(
                c,
                {destination_id(port, u) for u in todo if u in ok and u in tried},
                now,
            )
            started(c)

    try:
        while pending:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                pending.remove(task)
                received(task)
    finally:
        # A result may finish in the same loop turn that delivers cancellation. Keep it too.
        for task in pending:
            if task.done() and not task.cancelled() and task.exception() is None:
                received(task)
            else:
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return None if all(destination_id(port, u) in c.done for u in todo) else c

"""Route context from active incidents, without treating station overlap as train failure."""

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from train_commute_companion.calendar import to_utc
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.lifecycle import Transition, is_resolved, make_event
from train_commute_companion.model import (
    AlertEvent,
    AlertFacts,
    Ctx,
    EventKind,
    LegState,
    Obligation,
    Severity,
    alert_facts,
    okey,
)
from train_commute_companion.providers.base import ActiveDisruptions, ProviderAnswer
from train_commute_companion.state import (
    DisruptionNotice,
    DisruptionReceipt,
    OccurrenceState,
    forget_fact,
)
from train_commute_companion.store import PersistedState

LEGACY_PRIMARY = "legacy-primary"
PENDING_PRIMARY = "pending-primary"


class ReceiptPhase(StrEnum):
    UNANNOUNCED = "unannounced"
    CLEAR_OWED = "clear_owed"
    SETTLED = "settled"


def receipt_phase(receipt: DisruptionReceipt) -> ReceiptPhase:
    if receipt.needs_clear:
        return ReceiptPhase.CLEAR_OWED
    return ReceiptPhase.SETTLED if receipt.start_revision else ReceiptPhase.UNANNOUNCED


def receive_receipt(
    receipt: DisruptionReceipt, kind: EventKind, revision: int
) -> DisruptionReceipt:
    if type(revision) is not int or revision < 1 or revision <= receipt.notified_revision:
        return receipt
    if kind is EventKind.DISRUPTION:
        return replace(receipt, start_revision=revision, notified_revision=revision)
    if (
        kind is EventKind.DISRUPTION_CLEARED
        and receipt_phase(receipt) is ReceiptPhase.CLEAR_OWED
        and revision > receipt.start_revision
    ):
        return replace(receipt, clear_revision=revision, notified_revision=revision)
    return receipt


class NoticePhase(StrEnum):
    IRRELEVANT = "irrelevant"
    ACTIVE_PENDING = "active_pending"
    ACTIVE_ANNOUNCED = "active_announced"
    CLEAR_PENDING = "clear_pending"
    SETTLED = "settled"


def notice_phase(notice: DisruptionNotice) -> NoticePhase:
    if not notice.relevant:
        return NoticePhase.IRRELEVANT
    if notice.active:
        return NoticePhase.ACTIVE_ANNOUNCED if notice.notified else NoticePhase.ACTIVE_PENDING
    if any(receipt_phase(r) is ReceiptPhase.CLEAR_OWED for r in notice.receipts.values()) or (
        notice.audience is None and notice.announced and not notice.notified
    ):
        return NoticePhase.CLEAR_PENDING
    return NoticePhase.SETTLED


def context_enabled(state: OccurrenceState, ctx: Ctx) -> bool:
    return (
        not state.not_travelling
        and not is_resolved(state)
        and not ctx.profile.paused_on(state.key.service_date)
        and to_utc(ctx.schedule.window_start) <= to_utc(ctx.now) < to_utc(ctx.schedule.window_end)
    )


def disruptions_fresh(state: OccurrenceState, now: datetime) -> bool:
    return (
        state.disruptions_coverage == "complete"
        and state.disruptions_checked_at is not None
        and timedelta(0) <= to_utc(now) - to_utc(state.disruptions_checked_at) < FRESH_FOR
    )


def _stations(state: OccurrenceState, ctx: Ctx, answers: Sequence[ProviderAnswer]) -> set[str]:
    journeys = (*((state.lock,) if state.lock else ()), *state.retained)
    route = (
        set()
        if journeys
        else {
            ctx.profile.origin(ctx.direction),
            ctx.profile.destination(ctx.direction),
            *ctx.profile.journey(ctx.direction).transfers,
        }
    )
    for journey in journeys:
        for leg in journey.legs:
            if leg.state in (LegState.DONE, LegState.CANCELLED, LegState.RELOCKED):
                continue
            route.update((leg.board_station, leg.alight_stop))
            for answer in answers:
                for connection in answer.connections:
                    for current in connection.legs:
                        if current.train != leg.train or current.board.code != leg.board_station:
                            continue
                        for stop in current.stops:
                            route.add(stop.code)
                            if stop.uic:
                                route.add(stop.uic)
                            if stop.code == leg.alight_stop:
                                break
    return route


def notice_key(notice: DisruptionNotice, service_date: str) -> str:
    ident = hashlib.sha256(notice.item.id.encode()).hexdigest()[:24]
    kind = EventKind.DISRUPTION if notice.active else EventKind.DISRUPTION_CLEARED
    return f"{kind.value}:{service_date}:{ident}:{notice.revision}:{notice.instance}"


def notice_events(state: OccurrenceState, ctx: Ctx) -> tuple[AlertEvent, ...]:
    if not context_enabled(state, ctx) or not disruptions_fresh(state, ctx.now):
        return ()
    events = []
    for notice in state.disruptions.values():
        phase = notice_phase(notice)
        if phase not in (NoticePhase.ACTIVE_PENDING, NoticePhase.CLEAR_PENDING):
            continue
        targets = notice.audience
        if phase is NoticePhase.CLEAR_PENDING:
            targets = frozenset(
                destination
                for destination, receipt in notice.receipts.items()
                if receipt_phase(receipt) is ReceiptPhase.CLEAR_OWED
            )
            if notice.audience is None and notice.announced and not notice.notified:
                targets |= {LEGACY_PRIMARY}
        item = notice.item
        if phase is NoticePhase.ACTIVE_PENDING:
            text = f"NS reports {item.title} involving your route's stations; check your journey."
            kind, severity = EventKind.DISRUPTION, Severity.SILENT
        else:
            text = f"NS no longer lists {item.title} as active. Check your journey; this does not confirm that your trains run."
            kind, severity = EventKind.DISRUPTION_CLEARED, Severity.SILENT
        if item.url:
            text += f" {item.url}"
        facts = AlertFacts(
            disruption_id=item.id,
            disruption_revision=notice.revision,
            disruption_instance=notice.instance,
            disruption_targets=targets,
        )
        key = notice_key(notice, state.key.service_date.isoformat())
        if targets is not None and not notice.active:
            key += ":" + hashlib.sha256("|".join(sorted(targets)).encode()).hexdigest()[:16]
        events.append(
            make_event(
                ctx,
                kind,
                severity,
                text,
                key_facts=key,
                stated=facts,
            )
        )
    return tuple(events)


def update_disruptions(
    state: OccurrenceState,
    ctx: Ctx,
    feed: ActiveDisruptions,
    answers: Sequence[ProviderAnswer] = (),
    *,
    audience: frozenset[str] | None = None,
) -> Transition:
    if not context_enabled(state, ctx):
        return Transition(state, ())
    notices = dict(state.disruptions)
    route = _stations(state, ctx, answers)
    present = {item.id for item in feed.items}
    uncertain_scope = False
    for item in feed.items:
        previous = notices.get(item.id)
        if not item.stations:
            uncertain_scope |= previous is not None and previous.active and previous.relevant
            continue
        if not item.stations & route:
            if previous is not None and previous.relevant:
                notices[item.id] = replace(
                    previous,
                    item=item,
                    active=True,
                    revision=previous.revision + 1,
                    notified=True,
                    relevant=False,
                )
            continue
        if previous is None:
            notices[item.id] = DisruptionNotice(
                item, True, 1, instance=uuid4().hex, audience=audience
            )
        elif not previous.active or not previous.relevant or previous.item != item:
            notices[item.id] = replace(
                previous,
                item=item,
                active=True,
                revision=previous.revision + 1,
                notified=False,
                relevant=True,
                audience=audience,
                announced=previous.active and previous.relevant and previous.announced,
            )
    if feed.coverage == "complete":
        for ident, previous in notices.items():
            if previous.active and ident not in present:
                notices[ident] = replace(
                    previous,
                    active=False,
                    revision=previous.revision + 1,
                    notified=not previous.relevant or not previous.announced,
                )
    coverage = "unknown" if uncertain_scope else feed.coverage
    updated = replace(
        state, disruptions=notices, disruptions_checked_at=ctx.now, disruptions_coverage=coverage
    )
    return Transition(updated, notice_events(updated, ctx))


def disruptions_told(
    state: PersistedState, told: Sequence[tuple[Obligation, bool]]
) -> PersistedState:
    occurrences = dict(state.occurrences)
    for obligation, sent in told:
        if obligation.occurrence is None:
            continue
        facts = alert_facts(obligation.event)
        if not facts.disruption_id:
            continue
        key = okey(obligation.occurrence)
        occurrence = occurrences.get(key)
        notice = None if occurrence is None else occurrence.disruptions.get(facts.disruption_id)
        if occurrence is None or notice is None:
            continue
        if facts.disruption_instance != notice.instance:
            continue
        if notice.revision != facts.disruption_revision:
            if not (
                sent
                and obligation.event.kind is EventKind.DISRUPTION
                and 0 < facts.disruption_revision < notice.revision
            ):
                continue
            # A start can reach the phone after a newer feed has cleared its notice.
            needs_clear = not notice.active and notice.relevant and not notice.announced
            notice = replace(notice, announced=True, notified=notice.notified and not needs_clear)
        elif not sent:
            keys = dict(occurrence.alert_keys)
            forget_fact(keys, obligation.fact_key)
            occurrences[key] = replace(occurrence, alert_keys=keys)
            continue
        else:
            notice = replace(notice, notified=True, announced=notice.announced or notice.active)
        notices = {
            **occurrence.disruptions,
            facts.disruption_id: notice,
        }
        occurrences[key] = replace(occurrence, disruptions=notices)
    return replace(state, occurrences=occurrences)


def destination_allowed(notice: DisruptionNotice, event: AlertEvent, destination: str) -> bool:
    """Source truth is shared; acceptance and clearance eligibility belong to one recipient."""
    facts = alert_facts(event)
    if facts.disruption_instance != notice.instance:
        return False
    targets = facts.disruption_targets
    if targets is not None and destination not in targets:
        return False
    receipt = notice.receipts.get(destination, DisruptionReceipt())
    if event.kind is EventKind.DISRUPTION:
        # Legacy unpinned facts cannot acquire a newly configured email recipient.
        if targets is None and destination != LEGACY_PRIMARY:
            return False
        return receipt.notified_revision < facts.disruption_revision
    if event.kind is not EventKind.DISRUPTION_CLEARED:
        return False
    return (
        receipt_phase(receipt) is ReceiptPhase.CLEAR_OWED
        and receipt.clear_revision < facts.disruption_revision
    ) or (
        destination == LEGACY_PRIMARY
        and notice.audience is None
        and notice.announced
        and not notice.notified
    )


def disruptions_received(
    state: PersistedState,
    received: Sequence[tuple[Obligation, str]],
) -> PersistedState:
    """Merge actual acceptance into the latest source instance without restoring old state."""
    occurrences = dict(state.occurrences)
    for obligation, destination in received:
        if obligation.occurrence is None:
            continue
        facts = alert_facts(obligation.event)
        if not facts.disruption_id or facts.disruption_revision < 1:
            continue
        key = okey(obligation.occurrence)
        occurrence = occurrences.get(key)
        notice = None if occurrence is None else occurrence.disruptions.get(facts.disruption_id)
        if occurrence is None or notice is None or notice.instance != facts.disruption_instance:
            continue
        if facts.disruption_revision > notice.revision:
            continue
        if facts.disruption_targets is not None and destination not in facts.disruption_targets:
            continue
        receipt = notice.receipts.get(destination, DisruptionReceipt())
        receipt = receive_receipt(receipt, obligation.event.kind, facts.disruption_revision)
        notice = replace(notice, receipts={**notice.receipts, destination: receipt})
        occurrences[key] = replace(
            occurrence, disruptions={**occurrence.disruptions, facts.disruption_id: notice}
        )
    return replace(state, occurrences=occurrences)

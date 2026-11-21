"""Route context from active incidents, without treating station overlap as train failure."""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from uuid import uuid4

from train_commute_companion.calendar import TZ, to_utc
from train_commute_companion.catalogue import message, message_date
from train_commute_companion.classify import expected_departure
from train_commute_companion.health import FRESH_FOR
from train_commute_companion.lifecycle import Transition, is_resolved, make_event
from train_commute_companion.model import (
    AlertEvent,
    AlertFacts,
    Connection,
    Ctx,
    EventKind,
    LegState,
    Obligation,
    Severity,
    alert_facts,
    okey,
)
from train_commute_companion.providers.base import (
    ActiveDisruption,
    ActiveDisruptions,
    DisruptionSection,
    ProviderAnswer,
)
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


def _train_type(category: str) -> str | None:
    code = category.strip().upper()
    if code == "SPR":
        return "SPR"
    return "IC" if code.startswith("IC") else None  # IC, ICD, ICE, ICNG; anything else unknown


# per unfinished leg: its stations, its one train type (None when unknown), and every copy of
# its stops from boarding to alighting in order, each stop as its ids (None when any copy that
# shows the train does not list them all)
_Path = tuple[frozenset[str], ...]
_Leg = tuple[set[str], str | None, tuple[_Path, ...] | None]


def _legs(state: OccurrenceState, answers: Sequence[ProviderAnswer]) -> list[_Leg]:
    """The stations, train type and ordered stops of every unfinished locked or retained leg."""
    legs: list[_Leg] = []
    for journey in (*((state.lock,) if state.lock else ()), *state.retained):
        for leg in journey.legs:
            if leg.state in (LegState.DONE, LegState.CANCELLED, LegState.RELOCKED, LegState.MISSED):
                continue
            stations = {leg.board_station, leg.alight_stop}
            kinds = set()
            paths: list[_Path | None] = []
            for answer in answers:
                for connection in answer.connections:
                    for current in connection.legs:
                        if current.train != leg.train or current.board.code != leg.board_station:
                            continue
                        kinds.add(_train_type(current.category))
                        stops: list[frozenset[str]] = []
                        path = None
                        for stop in current.stops:
                            ids = frozenset(x for x in (stop.code, stop.uic) if x)
                            stations |= ids
                            stops.append(ids)
                            if stop.code == leg.alight_stop:
                                # only the two ends may be NS leaving the stops out (`_leg_stops`)
                                path = tuple(stops) if len(stops) > 2 else None
                                break
                        paths.append(path)
            known = tuple(x for x in paths if x is not None)
            whole = known if paths and len(known) == len(paths) else None
            legs.append((stations, kinds.pop() if len(kinds) == 1 else None, whole))
    return legs


def _stations(state: OccurrenceState, ctx: Ctx, legs: Sequence[_Leg]) -> set[str]:
    if state.lock or state.retained:
        return set().union(*(leg[0] for leg in legs))
    return {
        state.boarding_station or ctx.profile.origin(ctx.direction),
        ctx.profile.destination(ctx.direction),
        *ctx.profile.journey(ctx.direction).transfers,
    }


def _touches(section: DisruptionSection, leg: _Leg) -> bool:
    """A section is on your leg when it covers a stretch you travel (two of its stops in a row)
    or is one station you pass. Sharing only your end station is another line through it.
    Without the leg's stops, any shared station counts."""
    stations, _, paths = leg
    if paths is None:
        return bool(stations & section.stations)
    for path in paths:  # copies can differ (a diversion): it is on your leg if on any of them
        on = [bool(ids & section.stations) for ids in path]
        if any(a and b for a, b in pairwise(on)) or (section.size == 1 and any(on)):
            return True
    return False


def _hits_your_train(item: ActiveDisruption, legs: Sequence[_Leg]) -> bool:
    """False when no section is on your legs, or every one that is names one train type that
    none of your trains there is; any doubt (no lock, unknown type or wording) counts as a hit."""
    touched = False
    for section in item.sections:
        kinds = [leg[1] for leg in legs if _touches(section, leg)]
        if not kinds:
            continue
        touched = True
        if section.only is None or any(kind in (None, section.only) for kind in kinds):
            return True
    return not touched and not (legs and item.sections)


def for_connection(
    feed: ActiveDisruptions, connection: Connection, now: datetime
) -> tuple[ActiveDisruption, ...]:
    legs: list[_Leg] = []
    for leg in connection.legs:
        path = tuple(frozenset(x for x in (s.code, s.uic) if x) for s in leg.stops)
        legs.append(
            (set().union(*path), _train_type(leg.category), (path,) if len(path) > 2 else None)
        )
    route = set().union(*(leg[0] for leg in legs))
    dep = expected_departure(connection)
    return tuple(
        item
        for item in feed.items
        if item.stations & route
        and _hits_your_train(item, legs)
        and (item.end is None or to_utc(item.end) > max(to_utc(now), to_utc(dep)))
    )


def notice_key(notice: DisruptionNotice, service_date: str) -> str:
    ident = hashlib.sha256(notice.item.id.encode()).hexdigest()[:24]
    kind = EventKind.DISRUPTION if notice.active else EventKind.DISRUPTION_CLEARED
    return f"{kind.value}:{service_date}:{ident}:{notice.revision}:{notice.instance}"


def _active_text(item: ActiveDisruption, ctx: Ctx) -> str:
    language = ctx.profile.alerts.language
    until = ""
    if item.end is not None:
        end = item.end.astimezone(TZ)
        dated = end.date() != ctx.now.astimezone(TZ).date()
        day = f"{end.day} {end:%b}" if language == "en" else message_date(end.date(), language)
        until = message(
            "disruption.until",
            language,
            time=f"{end:%H:%M}",
            date=message("disruption.date", language, date=day) if dated else "",
        )
    if not item.summary:
        joined = f", {until}" if until else ""
        return message("disruption.active", language, title=item.title, until=joined)
    summary = item.summary if item.summary.endswith((".", "!", "?")) else item.summary + "."
    return message(
        "disruption.summary",
        language,
        summary=summary,
        until=f" {until[0].upper()}{until[1:]}." if until else "",
    )


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
        key = notice_key(notice, state.key.service_date.isoformat())
        if targets is not None and not notice.active:
            key += ":" + hashlib.sha256("|".join(sorted(targets)).encode()).hexdigest()[:16]
        if targets and notice.suppressed_destinations:
            targets -= notice.suppressed_destinations
            if not targets:
                continue
        item = notice.item
        if phase is NoticePhase.ACTIVE_PENDING:
            text = _active_text(item, ctx)
            kind, severity = EventKind.DISRUPTION, Severity.SILENT
        else:
            text = message("disruption.cleared", ctx.profile.alerts.language, title=item.title)
            kind, severity = EventKind.DISRUPTION_CLEARED, Severity.SILENT
        if item.url:
            text += f" {item.url}"
        facts = AlertFacts(
            disruption_id=item.id,
            disruption_revision=notice.revision,
            disruption_instance=notice.instance,
            disruption_targets=targets,
        )
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


def _same_news(told: ActiveDisruption, item: ActiveDisruption) -> bool:
    """A moved NS end time alone is not news, and a notice stored before 0.2.3 (no summary, end
    or sections) is the same notice as its newly parsed item."""
    if not told.summary and not told.sections:
        item = replace(item, summary="", sections=())
    elif not any(x.size for x in told.sections):  # stored before sections counted stations
        item = replace(item, sections=tuple(replace(x, size=0) for x in item.sections))
    return replace(told, end=None) == replace(item, end=None)


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
    legs = _legs(state, answers)
    route = _stations(state, ctx, legs)
    present = {item.id for item in feed.items}
    uncertain_scope = False
    for item in feed.items:
        previous = notices.get(item.id)
        if not item.stations:
            uncertain_scope |= previous is not None and previous.active and previous.relevant
            continue
        if not item.stations & route or not _hits_your_train(item, legs):
            if previous is not None and previous.relevant:
                notices[item.id] = replace(
                    previous,
                    item=item,
                    active=True,
                    revision=previous.revision + 1,
                    notified=True,
                    relevant=False,
                    suppressed_destinations=frozenset(),
                )
            continue
        if previous is None:
            notices[item.id] = DisruptionNotice(
                item, True, 1, instance=uuid4().hex, audience=audience
            )
        elif not previous.active or not previous.relevant or not _same_news(previous.item, item):
            notices[item.id] = replace(
                previous,
                item=item,
                active=True,
                revision=previous.revision + 1,
                notified=False,
                relevant=True,
                audience=audience,
                announced=previous.active and previous.relevant and previous.announced,
                suppressed_destinations=frozenset(),
            )
        else:
            notices[item.id] = replace(previous, item=item)
    if feed.coverage == "complete":
        for ident, previous in notices.items():
            if previous.active and ident not in present:
                notices[ident] = replace(
                    previous,
                    active=False,
                    revision=previous.revision + 1,
                    notified=not previous.relevant or not previous.announced,
                    suppressed_destinations=frozenset(),
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


def suppress_disruption_destinations(
    state: PersistedState,
    previous: PersistedState,
    disabled_profiles: set[str],
    destination: str | None,
    primary_scope: Callable[[str], bool],
) -> PersistedState:
    """Keep an opt-out with its source revision after an outbox obligation is gone."""
    changed = {}
    for key, occurrence in state.occurrences.items():
        old = previous.occurrences.get(key)
        disabled = occurrence.key.profile_id in disabled_profiles
        notices = {}
        for ident, notice in occurrence.disruptions.items():
            targets = notice.suppressed_destinations
            if disabled:
                pinned = (notice.audience or frozenset()) | notice.receipts.keys()
                targets |= {d for d in pinned if primary_scope(d)}
                if destination is not None:
                    targets |= {destination}
            prior = None if old is None else old.disruptions.get(ident)
            if (
                prior is not None
                and prior.instance == notice.instance
                and prior.revision == notice.revision
            ):
                targets |= prior.suppressed_destinations
            if targets != notice.suppressed_destinations:
                notices[ident] = replace(notice, suppressed_destinations=targets)
        if notices:
            changed[key] = replace(occurrence, disruptions={**occurrence.disruptions, **notices})
    return replace(state, occurrences={**state.occurrences, **changed}) if changed else state


def destination_allowed(notice: DisruptionNotice, event: AlertEvent, destination: str) -> bool:
    """Source truth is shared; acceptance and clearance eligibility belong to one recipient."""
    facts = alert_facts(event)
    if facts.disruption_instance != notice.instance:
        return False
    if (
        facts.disruption_revision == notice.revision
        and destination in notice.suppressed_destinations
    ):
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

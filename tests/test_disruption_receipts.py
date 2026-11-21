"""Disruption receipts belong to the exact recipient and source instance that was sent."""

import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta

import pytest
import runtime_seam as seam
from lifecycle_fixtures import at, ctx, usual_locked
from runtime_rig import PROFILE, Rig

from train_commute_companion.disruptions import disruptions_received, update_disruptions
from train_commute_companion.model import AlertFacts, EventKind, Obligation, okey
from train_commute_companion.providers.base import ActiveDisruption, ActiveDisruptions
from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import dumps, loads
from train_commute_companion.store import PersistedState
from train_commute_companion.webhook import url_id

START, CLEAR = EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED
EMAIL, OTHER = "email:one", "email:two"


def feed(active=True, title="Signal fault", coverage="complete"):
    items = (ActiveDisruption("incident", title, frozenset({"UT"}), None),) if active else ()
    return ActiveDisruptions(items, coverage)


class Aux:
    def __init__(self, now, records):
        self.urls = (EMAIL, OTHER)
        self.channels = dict.fromkeys(self.urls, "email")
        self.destination_ids = {u: url_id(u) for u in self.urls}
        self.down = {}
        self.on_reserve = lambda: None
        self.now, self.records = now, records
        self.fail = set()
        self.gate = None
        self.entered = asyncio.Event()

    def due(self, urls):
        return frozenset(u for u in urls if self.down.get(u, self.now()) <= self.now())

    async def post(self, event, msg_id, urls):
        tried, accepted = self.due(urls), set()
        for destination in tried:
            self.down[destination] = self.now() + timedelta(seconds=60)
            self.on_reserve()
            self.records[destination].append(event)
            if self.gate is not None and destination == EMAIL and event.kind is START:
                self.entered.set()
                await self.gate.wait()
            if destination not in self.fail or event.kind not in (START, CLEAR):
                accepted.add(destination)
                self.down.pop(destination)
                self.on_reserve()
        return tried, frozenset(accepted)


class World:
    def __init__(self, tmp_path):
        self.rig = Rig(tmp_path, "08:04")
        self.rig.provider.disruptions = feed()
        self.records = defaultdict(list)
        self.primary = "opaque-phone-one"
        self.build()

    def build(self):
        rig = self.rig
        self.port = Aux(rig.clock.now, self.records)
        rig.rt = Runtime(
            profile_path=rig.path,
            store=rig.store,
            provider=rig.provider,
            notifier=rig.notifier,
            clock=rig.clock,
            require_stations=False,
            webhooks=self.port,
            primary_destination=lambda: self.primary,
        )

    async def start(self):
        await self.rig.rt.start()
        await self.rig.rt.webhooks_idle()

    async def poll(self, active=True, **kwargs):
        self.rig.clock.jump(2)
        self.rig.provider.disruptions = feed(active, **kwargs)
        await self.rig.rt.poll_once()
        await self.rig.rt.webhooks_idle()

    def kinds(self, destination=EMAIL):
        return [e.kind for e in self.records[destination] if e.kind in (START, CLEAR)]

    def notice(self):
        return self.rig.occ().disruptions["incident"]


def history_setting(world, value="true"):
    world.rig.write(
        text=PROFILE.replace("NAME", "office_a").replace(
            "    outbound:", f"    alerts: {{alert_history: {value}}}\n    outbound:", 1
        )
    )


def start_rows(world):
    return tuple(row for row in world.rig.rt.recent_alerts() if row.kind is START)


@pytest.mark.parametrize("first", ["phone", "email"])
async def test_history_preserves_first_accepted_text_across_recipient_retries(tmp_path, first):
    world = World(tmp_path)
    history_setting(world)
    world.rig.notifier.fails = lambda e: first == "email" and e.kind is START
    world.port.fail = {OTHER} if first == "email" else {EMAIL, OTHER}
    await world.start()
    (row,) = start_rows(world)
    receipt = world.records[EMAIL] if first == "email" else world.rig.rt.events_log
    assert row.text == next(e.text for e in receipt if e.kind is START)
    assert row.accepted_at == world.rig.clock.now()

    world.rig.path.write_text(world.rig.path.read_text().replace("office_a", "renamed"))
    world.rig.clock.jump(2)
    world.rig.notifier.fails = lambda e: False
    world.build()
    await world.start()
    assert world.kinds(OTHER) == [START, START]
    assert "Renamed" in world.records[OTHER][-1].text
    assert start_rows(world) == (row,)
    assert world.rig.store.load().alert_history == world.rig.rt.state.alert_history


async def test_history_is_off_by_default_and_hides_rows_without_backfilling(tmp_path):
    world = World(tmp_path)
    await world.start()
    assert world.kinds() == [START]
    assert not world.rig.store.load().alert_history

    history_setting(world)
    assert await world.rig.rt.reload() is None
    assert not world.rig.rt.recent_alerts()
    await world.poll(title="Expanded fault")
    (row,) = start_rows(world)

    history_setting(world, "false")
    assert await world.rig.rt.reload() is None
    await world.poll(title="Changed again")
    assert world.kinds() == [START, START, START]
    assert world.rig.store.load().alert_history == (row,)
    assert not world.rig.rt.recent_alerts()
    history_setting(world)
    assert await world.rig.rt.reload() is None
    assert start_rows(world) == (row,)


async def test_history_persists_only_user_facing_fields_and_omits_station_and_app_alerts(
    tmp_path,
):
    world = World(tmp_path)
    history_setting(world)
    await world.start()
    before = {r.text for r in world.rig.rt.recent_alerts()}
    start = next(e for e in world.rig.rt.events_log if e.kind is START)
    messages = {
        EventKind.AT_STATION: "private detected station",
        EventKind.STATION_ASK: "private station question",
        EventKind.STATION_REMEMBER: "private station confirmation",
        EventKind.KEY_REJECTED: "private app warning",
        EventKind.CANCELLED: "Your train was cancelled.",
    }
    current = world.rig.rt.state.obligations
    obligations = tuple(
        seam.obligation(
            world.rig.rt,
            replace(
                start,
                profile_id="" if kind is EventKind.KEY_REJECTED else start.profile_id,
                kind=kind,
                key=text,
                text=text,
                facts=AlertFacts(prompt_id="private-button-callback"),
            ),
            None,
            world.rig.clock.now(),
        )
        for kind, text in messages.items()
    )
    seam.seed_state(world.rig.rt, obligations=(*current, *obligations))
    await seam.deliver(world.rig.rt)
    await world.rig.rt.webhooks_idle()
    assert {e.text for e in world.rig.rt.events_log} >= set(messages.values())
    saved = json.loads(world.rig.store.text)["alert_history"]
    assert len(saved) == len(before) + 1
    assert {r["text"] for r in saved} == before | {messages[EventKind.CANCELLED]}
    assert all(
        r.keys() == {"id", "profile_id", "kind", "created_at", "accepted_at", "text"} for r in saved
    )
    assert "private-" not in json.dumps(saved) and world.primary not in json.dumps(saved)


async def test_history_caps_rows_without_resurrecting_evicted_alerts_and_expires(tmp_path):
    world = World(tmp_path)
    history_setting(world)
    world.port.fail = {EMAIL}
    await world.start()
    (first,) = start_rows(world)
    world.rig.clock.jump(2)
    now = world.rig.clock.now()
    newer = tuple(
        replace(first, id=f"row-{i:03}", created_at=now, accepted_at=now) for i in range(100)
    )
    rt = world.rig.rt
    seam.seed_state(rt, alert_history=(first, *newer))
    seam.save_state(rt)
    assert rt.recent_alerts() == newer[::-1]

    world.port.fail.clear()
    await rt.watchdog_once()
    await rt.webhooks_idle()
    assert world.kinds() == [START, START]
    assert rt.recent_alerts() == newer[::-1]
    world.rig.clock.jump(30 * 24 * 60)
    assert not rt.recent_alerts()
    await rt.watchdog_once()
    assert all(row.created_at > now for row in world.rig.store.load().alert_history)


async def test_primary_failure_does_not_prevent_email_start_and_clear(tmp_path):
    world = World(tmp_path)
    world.rig.notifier.fails = lambda e: e.kind is START
    world.port.fail = {OTHER}
    await world.start()
    assert world.notice().receipts[url_id(EMAIL)].needs_clear
    await world.poll(False)
    assert world.kinds() == [START, CLEAR]
    assert CLEAR not in world.kinds(OTHER)
    assert CLEAR not in [e.kind for e in world.rig.notifier.attempts]
    assert not world.notice().receipts[url_id(EMAIL)].needs_clear


async def test_primary_success_does_not_invalidate_owed_email_start(tmp_path):
    world = World(tmp_path)
    world.port.fail = {EMAIL}
    await world.start()
    assert world.notice().notified
    world.port.fail.clear()
    world.rig.clock.jump(61 / 60)
    await world.rig.rt.watchdog_once()
    await world.rig.rt.webhooks_idle()
    assert world.kinds() == [START, START]
    assert world.notice().receipts[url_id(EMAIL)].needs_clear
    await world.poll(False)
    assert world.kinds() == [START, START, CLEAR]


async def test_unsent_stale_start_never_receives_clear(tmp_path):
    world = World(tmp_path)
    world.port.down[EMAIL] = world.rig.clock.now() + timedelta(minutes=10)
    await world.start()
    await world.poll(False)
    assert world.kinds() == []
    assert world.kinds(OTHER) == [START, CLEAR]


async def test_late_email_start_gets_targeted_clear_after_primary_clear_finished(tmp_path):
    world = World(tmp_path)
    world.port.gate = asyncio.Event()
    await world.rig.rt.start()
    await asyncio.wait_for(world.port.entered.wait(), 2)
    world.rig.clock.jump(2)
    world.rig.provider.disruptions = feed(False)
    await world.rig.rt.poll_once()
    assert CLEAR in [e.kind for e in world.rig.notifier.attempts]
    world.port.gate.set()
    await world.rig.rt.webhooks_idle()
    # OTHER can still owe its clearance while EMAIL's late start is acknowledged.
    # Both recipients saw the start. The phone already accepted its clearance.
    candidates = [
        o.event.facts
        for o in world.rig.rt.state.obligations
        if o.event.kind is CLEAR and url_id(EMAIL) in o.event.facts.disruption_targets
    ]
    assert candidates
    assert all(f.disruption_targets <= {url_id(EMAIL), url_id(OTHER)} for f in candidates)
    assert all(f.disruption_instance == world.notice().instance for f in candidates)
    assert all(f.disruption_revision == world.notice().revision for f in candidates)
    await world.rig.rt.watchdog_once()
    await world.rig.rt.webhooks_idle()
    assert world.kinds() == [START, CLEAR]
    assert world.kinds(OTHER) == [START, CLEAR]


async def test_unknown_coverage_does_not_clear_and_receipts_survive_restart(tmp_path):
    world = World(tmp_path)
    await world.start()
    await world.poll(False, coverage="unknown")
    assert world.kinds() == [START]
    world.build()
    await world.start()
    assert world.kinds() == [START]
    await world.poll(False)
    assert world.kinds() == [START, CLEAR]


async def test_new_primary_recipient_does_not_inherit_old_clearance(tmp_path):
    world = World(tmp_path)
    await world.start()
    world.primary = "opaque-phone-two"
    world.build()
    await world.start()
    world.rig.notifier.attempts.clear()
    await world.poll(False)
    assert CLEAR not in [e.kind for e in world.rig.notifier.attempts]
    assert world.kinds() == [START, CLEAR]


def obligation(event, state):
    return Obligation(
        "sent", event, state.key, None, state.route_revision, None, event.key, None, 0
    )


@pytest.mark.parametrize("changed", ["updated", "reactivated", "removed", "replaced"])
def test_receipt_merges_latest_source_without_recreating_or_acknowledging_new_revision(changed):
    first = update_disruptions(
        usual_locked(), ctx(at("07:50")), feed(), audience=frozenset({"recipient"})
    )
    original = first.state.disruptions["incident"]
    event = first.events[0]
    if changed == "reactivated":
        cleared = update_disruptions(first.state, ctx(at("07:51")), feed(False))
        event = replace(event, kind=CLEAR, facts=replace(event.facts, disruption_revision=2))
        latest = update_disruptions(
            cleared.state, ctx(at("07:52")), feed(title="New active fault")
        ).state
    else:
        latest = update_disruptions(
            first.state, ctx(at("07:51")), feed(title="Expanded fault")
        ).state
    if changed == "replaced":
        latest = replace(
            latest, disruptions={"incident": replace(original, instance="another-instance")}
        )
    key = okey(latest.key)
    state = PersistedState(
        None, {}, {}, {}, {} if changed == "removed" else {key: latest}, (), False
    )
    result = disruptions_received(state, [(obligation(event, first.state), "recipient")])
    if changed in ("removed", "replaced"):
        assert result == state
    else:
        notice = result.occurrences[key].disruptions["incident"]
        assert notice.active and not notice.notified
        assert notice.revision == latest.disruptions["incident"].revision
        assert notice.receipts["recipient"].notified_revision < notice.revision
        assert loads(type(result), dumps(result)) == result


async def test_receipt_and_copy_progress_recover_together_after_failed_write(tmp_path):
    world = World(tmp_path)
    history_setting(world)
    world.port.gate = asyncio.Event()
    world.port.fail = {OTHER}
    world.rig.notifier.fails = lambda e: e.kind is START
    await world.rig.rt.start()
    await asyncio.wait_for(world.port.entered.wait(), 2)
    world.rig.store.fail = True
    world.port.gate.set()
    await world.rig.rt.webhooks_idle()
    assert not world.rig.rt.view().store_ok
    assert not start_rows(world)
    assert all(row.kind is not START for row in world.rig.store.load().alert_history)
    world.rig.store.fail = False
    await world.rig.rt.watchdog_once()
    await world.rig.rt.webhooks_idle()
    assert world.notice().receipts[url_id(EMAIL)].needs_clear
    (row,) = start_rows(world)
    assert row.text == next(e.text for e in world.records[EMAIL] if e.kind is START)
    world.build()
    await world.start()
    assert world.kinds() == [START]
    assert start_rows(world) == (row,)
    await world.poll(False)
    assert world.kinds() == [START, CLEAR]


async def test_skip_during_start_delivery_does_not_restore_travel_or_create_clearance(tmp_path):
    world = World(tmp_path)
    world.port.gate = asyncio.Event()
    await world.rig.rt.start()
    await asyncio.wait_for(world.port.entered.wait(), 2)
    assert await world.rig.rt.not_travelling("p1") is None
    world.port.gate.set()
    await world.rig.rt.webhooks_idle()
    assert world.rig.occ().not_travelling
    await world.poll(False)
    assert world.rig.occ().not_travelling
    assert world.kinds() == [START]
    assert not any(o.event.kind is CLEAR for o in world.rig.rt.state.obligations)


def test_legacy_clearance_does_not_claim_unknown_recipient_receipts():
    from train_commute_companion.disruptions import (
        LEGACY_PRIMARY,
        destination_allowed,
        notice_events,
    )
    from train_commute_companion.state import DisruptionNotice

    original = usual_locked()
    old = DisruptionNotice(feed().items[0], False, 2, announced=True)
    legacy = replace(
        original,
        disruptions={"incident": old},
        disruptions_checked_at=at("07:50"),
        disruptions_coverage="complete",
    )
    (event,) = notice_events(legacy, ctx(at("07:50")))
    assert destination_allowed(old, event, LEGACY_PRIMARY)
    assert not destination_allowed(old, event, "new-phone")
    assert not destination_allowed(old, event, url_id(EMAIL))


def test_receipt_transitions_require_start_before_later_clear():
    from train_commute_companion.disruptions import ReceiptPhase, receipt_phase, receive_receipt
    from train_commute_companion.state import DisruptionReceipt

    receipt = DisruptionReceipt()
    assert receipt_phase(receipt) is ReceiptPhase.UNANNOUNCED
    assert receive_receipt(receipt, CLEAR, 3) == receipt
    receipt = receive_receipt(receipt, START, 2)
    assert receipt_phase(receipt) is ReceiptPhase.CLEAR_OWED
    assert receive_receipt(receipt, CLEAR, 1) == receipt
    cleared = receive_receipt(receipt, CLEAR, 3)
    assert receipt_phase(cleared) is ReceiptPhase.SETTLED
    assert receive_receipt(cleared, START, 2) == cleared
    assert receipt_phase(receive_receipt(cleared, START, 4)) is ReceiptPhase.CLEAR_OWED


def test_incident_phase_keeps_source_truth_separate_from_recipient_clearance():
    from train_commute_companion.disruptions import NoticePhase, notice_phase
    from train_commute_companion.state import DisruptionNotice, DisruptionReceipt

    notice = DisruptionNotice(feed().items[0], True, 1)
    assert notice_phase(notice) is NoticePhase.ACTIVE_PENDING
    told = replace(notice, notified=True, announced=True)
    assert notice_phase(told) is NoticePhase.ACTIVE_ANNOUNCED
    cleared = replace(told, active=False, revision=2, receipts={"one": DisruptionReceipt(1, 0, 1)})
    assert notice_phase(cleared) is NoticePhase.CLEAR_PENDING
    assert notice_phase(replace(cleared, relevant=False)) is NoticePhase.IRRELEVANT
    assert notice_phase(replace(cleared, receipts={})) is NoticePhase.SETTLED

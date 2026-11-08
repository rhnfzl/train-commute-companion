"""Delivery phases must drive decisions and reject premature acknowledgements."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from lifecycle_fixtures import full_state
from test_delivery import NOW, View, ob

from train_commute_companion import budget, delivery, health, news
from train_commute_companion.model import EventKind, Severity, WebhookCopy
from train_commute_companion.providers.base import AuthError, RateLimited
from train_commute_companion.state import PollRecord
from train_commute_companion.store import JsonFileStore, StorageError, read_state
from train_commute_companion.webhook_send import WebhookPoster


def test_future_poll_success_cannot_make_a_board_fresh():
    future = PollRecord(NOW + timedelta(minutes=1), True, None)
    result = health.board_health([future], NOW)
    assert not result.fresh
    assert health.offline(result, False)


@pytest.mark.parametrize("version", [True, False, 1.0, 2.0])
def test_store_version_requires_an_integer_identity(version):
    with pytest.raises(ValueError, match="version"):
        read_state(version, {})


def test_negative_store_counter_is_quarantined(tmp_path):
    path = tmp_path / "state.json"
    store = JsonFileStore(path)
    store.save(full_state())
    raw = json.loads(path.read_text())
    raw["write_counter"] = -1
    path.write_text(json.dumps(raw))
    with pytest.raises(StorageError, match="counter"):
        JsonFileStore(path).load()
    assert list(tmp_path.glob("state.json.broken-*"))


def test_request_budget_has_separate_live_and_optional_resume_phases():
    phase = budget.BudgetPhase
    b = budget.RequestBudget().on_result(RateLimited("rate"), NOW, None)
    assert b.phase(NOW) is phase.COOLDOWN
    assert not b.allow(NOW, live=True)
    at_end = NOW + timedelta(minutes=2)
    assert b.phase(at_end) is phase.LIVE_ONLY
    assert b.allow(at_end, live=True) and not b.allow(at_end, live=False)
    assert b.phase(at_end + timedelta(minutes=2)) is phase.OPEN
    auth = b.on_result(AuthError("auth"), at_end, None)
    assert auth.backoff_step == 1 and not auth.allow(at_end, live=True)
    assert auth.on_result(None, at_end, None).phase(at_end) is phase.OPEN


def test_typed_obligation_verdict_drives_hold_drop_and_send():
    verdict = delivery.DeliveryVerdict
    o = ob(EventKind.DELAYED, Severity.SILENT)
    assert delivery.revalidate(o, View(quiet=True), NOW) is verdict.HOLD
    assert delivery.revalidate(o, View(holds=False), NOW) is verdict.DROP
    assert delivery.revalidate(o, View(), NOW) is verdict.SEND


def test_binding_and_primary_verdict_distinguish_pending_and_wrong_target():
    phase = delivery.BindingPhase
    o = ob(EventKind.DELAYED)
    assert delivery.binding_phase(o) is phase.UNBOUND
    assert (
        delivery.primary_delivery_verdict(
            replace(o, primary_target_pending=True), View(), NOW, "phone", True
        )
        is delivery.DeliveryVerdict.HOLD
    )
    pending = replace(o, target_ids=frozenset(), primary_target_pending=True)
    assert delivery.binding_phase(pending) is phase.PRIMARY_PENDING
    assert (
        delivery.primary_delivery_verdict(pending, View(), NOW, "phone", True)
        is delivery.DeliveryVerdict.HOLD
    )
    pinned = replace(pending, target_ids=frozenset({"phone"}), primary_target_pending=False)
    assert delivery.binding_phase(pinned) is phase.BOUND
    assert (
        delivery.primary_delivery_verdict(pinned, View(), NOW, "replacement", True)
        is delivery.DeliveryVerdict.DROP
    )
    assert (
        delivery.primary_delivery_verdict(pinned, View(), NOW, "phone", False)
        is delivery.DeliveryVerdict.HOLD
    )
    assert (
        delivery.primary_delivery_verdict(pinned, View(), NOW, "phone", True)
        is delivery.DeliveryVerdict.SEND
    )


def test_copy_receipts_only_record_pinned_destinations_and_monotone_tries():
    o = replace(ob(EventKind.DELAYED), target_ids=frozenset({"one", "two"}))
    c = WebhookCopy("copy", o, target_ids=o.target_ids)
    assert delivery.copy_phase(c) is delivery.CopyPhase.QUEUED
    c = delivery.start_copy(c, NOW)
    assert delivery.copy_phase(c) is delivery.CopyPhase.STARTED
    c = delivery.receive_copy(c, {"one", "foreign"}, NOW)
    assert c.done == ("one",)
    assert delivery.copy_phase(c) is delivery.CopyPhase.PARTIAL
    earlier = NOW - timedelta(minutes=1)
    c = delivery.receive_copy(c, {"one"}, earlier)
    assert c.done == ("one",) and c.last_try == NOW
    c = delivery.receive_copy(c, {"two"}, NOW)
    assert delivery.copy_phase(c) is delivery.CopyPhase.COMPLETE


def test_fact_announcement_phases_control_recurrence_and_escalation():
    assert news.announcement_phase(None) is news.AnnouncementPhase.UNANNOUNCED
    assert news.announcement_phase(Severity.SILENT) is news.AnnouncementPhase.SILENT
    assert news.announcement_phase(Severity.LOUD) is news.AnnouncementPhase.LOUD
    event = ob(EventKind.DELAYED).event
    assert news.unsent({}, [event]) == [event]
    assert news.unsent({event.key: Severity.SILENT}, [event]) == []


async def test_destination_in_flight_cannot_be_retried_after_clock_jump():
    gate = asyncio.Event()
    entered = asyncio.Event()
    now = NOW
    calls = 0

    async def receive(request):
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        poster = WebhookPoster(client, ("https://example.invalid/a",), "test", lambda: now)
        first = asyncio.create_task(poster.post(ob(EventKind.DELAYED).event, "id", poster.urls))
        await entered.wait()
        now += timedelta(minutes=2)
        assert not poster.due(poster.urls)
        assert await poster.post(ob(EventKind.DELAYED).event, "id", poster.urls) == (
            frozenset(),
            frozenset(),
        )
        gate.set()
        assert (await first)[1] == frozenset(poster.urls)
        assert calls == 1


async def test_copy_scheduler_coalesces_requests_and_stop_is_terminal(monkeypatch):
    from train_commute_companion.webhook_queue import CopyQueue, QueuePhase

    entered, release = asyncio.Event(), asyncio.Event()
    passes = []
    port = object()
    # Construct with no poster, then attach a harmless port after callback wiring.
    q = CopyQueue(None, lambda: NOW, lambda _: None, lambda _: False, lambda _: View())
    q.port = port

    async def run_pass(p):
        passes.append(p)
        if len(passes) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(q, "_post_copies", run_pass)
    q.post_soon()
    await entered.wait()
    assert q.phase is QueuePhase.RUNNING
    q.post_soon()
    q.post_soon()
    assert q.phase is QueuePhase.RERUN_REQUESTED
    release.set()
    await q.idle()
    assert passes == [port, port]
    assert q.phase is QueuePhase.IDLE
    await q.stop()
    assert q.phase is QueuePhase.STOPPED
    q.post_soon()
    await asyncio.sleep(0)
    assert passes == [port, port]


def test_store_history_conflict_requires_explicit_reload(tmp_path):
    path = tmp_path / "state.json"
    mine, other = JsonFileStore(path), JsonFileStore(path)
    mine.save(full_state())
    other.load()
    other.save(full_state())
    with pytest.raises(StorageError):
        mine.save(full_state())
    # Counter equality alone must not restore trust after observing another writer.
    raw = json.loads(path.read_text())
    raw["write_counter"] = 1
    path.write_text(json.dumps(raw))
    with pytest.raises(StorageError, match="reload"):
        mine.save(full_state())
    mine.load()
    mine.save(full_state())


def test_counter_check_and_replace_are_one_owned_transaction(tmp_path, monkeypatch):
    import os
    import threading

    path = tmp_path / "state.json"
    first, second = JsonFileStore(path), JsonFileStore(path)
    first.save(full_state())
    second.load()
    ready, release, second_started, second_done = (threading.Event() for _ in range(4))
    original = os.replace
    failures = {}

    def paused_replace(source, destination):
        if destination == path and threading.current_thread().name == "first-store-writer":
            ready.set()
            assert release.wait(5)
        return original(source, destination)

    def write(name, store, candidate):
        if name == "second":
            second_started.set()
        try:
            store.save(candidate)
        except StorageError as error:
            failures[name] = error
        finally:
            if name == "second":
                second_done.set()

    monkeypatch.setattr(os, "replace", paused_replace)
    a = threading.Thread(
        target=write,
        args=("first", first, replace(full_state(), started_before=False)),
        name="first-store-writer",
    )
    b = threading.Thread(target=write, args=("second", second, full_state()))
    a.start()
    assert ready.wait(5)
    b.start()
    try:
        assert second_started.wait(5)
        assert second_done.wait(0.5), "a competing lock owner blocked the runtime"
        assert "busy" in str(failures["second"])
        from train_commute_companion.store import StorePhase

        assert second.phase is StorePhase.READY
    finally:
        release.set()
        a.join(5)
        b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert set(failures) == {"second"}
    assert JsonFileStore(path).load().started_before is False


def test_visibility_episode_transitions_emit_once_and_preserve_outage_identity():
    from train_commute_companion.ledger import EpisodeTransition, PollMemory

    memory = PollMemory()
    change = memory.observe_blind("journey", True, NOW)
    assert change.transition is EpisodeTransition.ENTERED and change.since == NOW
    assert (
        memory.observe_blind("journey", True, NOW + timedelta(minutes=2)).transition
        is EpisodeTransition.UNCHANGED
    )
    recovered = memory.observe_blind("journey", False, NOW + timedelta(minutes=4))
    assert recovered.transition is EpisodeTransition.EXITED and recovered.since == NOW
    assert not memory.blind
    assert memory.observe_offline("journey", True) is EpisodeTransition.ENTERED
    assert memory.observe_offline("journey", True) is EpisodeTransition.UNCHANGED
    assert memory.observe_offline("journey", False) is EpisodeTransition.EXITED
    assert memory.observe_offline("journey", False) is EpisodeTransition.UNCHANGED
    memory.observe_blind("journey", True, NOW)
    memory.observe_offline("journey", True)
    memory.forget("journey")
    assert not memory.blind and not memory.offline


async def test_one_destination_has_one_reservation_when_input_repeats():
    received = []

    def accept(request):
        received.append(request)
        return httpx.Response(200)

    url = "https://example.invalid/a"
    async with httpx.AsyncClient(transport=httpx.MockTransport(accept)) as client:
        poster = WebhookPoster(client, (url,), "test", lambda: NOW)
        assert await poster.post(ob(EventKind.DELAYED).event, "id", (url, url)) == (
            frozenset({url}),
            frozenset({url}),
        )
        assert len(received) == 1

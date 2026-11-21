import functools
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from app_rig import CHAT, CODES, TickClock, make_app
from builders import make_connection, make_leg
from runtime_rig import DAY, Provider, StationsProvider, at, every_day
from tcc_app.ha_delivery import HaDelivery, HaDeliverySettings
from tcc_app.web import start_web

from train_commute_companion.compensation_state import JourneyRecord, PlannedJourney
from train_commute_companion.home_assistant_bridge import (
    PATH,
    VERSION,
    dispatch,
    read_result,
    request,
)
from train_commute_companion.messages import Refused
from train_commute_companion.model import (
    AlertEvent,
    Direction,
    EventKind,
    OccurrenceKey,
    Severity,
    okey,
)
from train_commute_companion.notify import NotificationPaused
from train_commute_companion.providers.base import ActiveDisruption, ActiveDisruptions
from train_commute_companion.schedule_migration import merge_profile_history
from train_commute_companion.serde import decode

TOKEN = "0" * 64
HA_DESTINATION = "ha-mobile:" + "a" * 64
HA_ALERT_URL = "https://ha.example/api/train_commute_companion/bridge-alert"


async def test_bridge_auth_and_locked_view(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, StationsProvider(Provider(clock, every_day()), CODES))
    await app.rt.start()
    await app.rt.poll_once()
    view = app.rt.view()
    assert view.occurrences and view.occurrences[0].legs
    server = await start_web(
        0,
        health=lambda: (200, {"ok": True}),
        host="127.0.0.1",
        home_assistant_token=TOKEN,
        home_assistant=functools.partial(dispatch, app.rt),
    )
    try:
        async with httpx.AsyncClient() as client:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}{PATH}"
            assert (await client.post(url, json=request("view", {}))).status_code == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            invalid = await client.post(url, json=request("run_forever", {}), headers=headers)
            assert invalid.status_code == 400
            response = await client.post(url, json=request("view", {}), headers=headers)
            assert response.status_code == 200
            assert read_result("view", response.json()) == view
    finally:
        server.close()
        await server.wait_closed()


async def test_household_sync_updates_only_the_existing_profile(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, StationsProvider(Provider(clock, every_day()), CODES))
    await app.rt.start()
    before = app.rt.view().profiles[0]
    day = DAY + timedelta(days=3)
    args = {
        "profile_id": before.id,
        "extra_dates": (day,),
        "outbound_buffer_min": 31,
        "return_buffer_min": 32,
    }
    result = await dispatch(app.rt, request("sync_profile", args))
    assert read_result("sync_profile", result) is None
    updated = app.rt.view().profiles[0]
    assert (updated.id, updated.home_station, updated.work_station) == (
        before.id,
        before.home_station,
        before.work_station,
    )
    assert updated.extra_dates == frozenset({day})
    assert (updated.outbound.buffer_min, updated.return_.buffer_min) == (31, 32)


async def test_household_sync_refuses_a_new_overlap_without_touching_the_plan(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, StationsProvider(Provider(clock, every_day()), CODES))
    await app.rt.start()
    sunday = DAY + timedelta(days=(7 - DAY.isoweekday()) or 7)
    change = {
        "action": "create",
        "plan": {
            "name": "Sunday Utrecht",
            "origin": "AAA",
            "destination": "UT",
            "start_date": sunday.isoformat(),
            "outbound": {"mode": "window", "arrive_by": "09:00"},
        },
    }
    preview = app.rt.preview_travel_plan(change)
    assert preview["status"] == "ready"
    saved = app.rt.save_travel_plan(change, preview["review_token"], "sunday-trip")
    assert saved["status"] == "saved"
    before = app.rt.state
    path = app.settings.profiles
    original = path.read_bytes()
    refusal = await app.rt.sync_profile(before.config.profiles[0].id, (sunday,), 21, 22)
    assert refusal is not None and "Sunday Utrecht" in refusal.text
    assert path.read_bytes() == original
    assert app.rt.state.travel_plans == before.travel_plans
    assert app.rt.state.travel_receipts == before.travel_receipts


async def test_bridge_delivers_the_full_event_and_preserves_gates():
    event = AlertEvent(
        "p1",
        "office",
        Direction.OUTBOUND,
        DAY,
        None,
        EventKind.LEAVE_NOW,
        Severity.LOUD,
        None,
        "Leave now",
        "p1|test",
        at("07:50"),
    )
    fallback = []
    delivered = []
    paused = False
    precondition_failed = False
    target = HA_DESTINATION

    class Default:
        async def send(self, message):
            fallback.append(message)
            return True

    def receive(call):
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        if call.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": VERSION,
                    "destination": target,
                    "wake_destination": target,
                    "wake_ready": True,
                },
            )
        assert call.headers["X-TCC-Destination"] == HA_DESTINATION
        if precondition_failed:
            return httpx.Response(412)
        body = json.loads(call.content)
        assert body["version"] == VERSION
        delivered.append(decode(AlertEvent, body["event"]))
        return httpx.Response(200, json={"accepted": not paused, "paused": paused})

    settings = HaDeliverySettings(HA_ALERT_URL, frozenset({"p1"}), None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        bridge = HaDelivery(settings, TOKEN, client, Default())
        await bridge.refresh()
        assert bridge.destination == HA_DESTINATION
        assert bridge.wake_destination == HA_DESTINATION
        assert await bridge.send(event)
        assert await bridge.send(replace(event, profile_id="day-trip"))
        outage = replace(event, profile_id="", kind=EventKind.OFFLINE)
        assert await bridge.send(outage)
        assert delivered == [event, outage]
        assert [message.profile_id for message in fallback] == ["day-trip"]
        paused = True
        with pytest.raises(NotificationPaused):
            await bridge.send(event)

        precondition_failed = True
        with pytest.raises(NotificationPaused):
            await bridge.send_to(event, HA_DESTINATION)
        assert bridge.destination is None
        assert bridge.wake_destination is None

        target = "ha-mobile:" + "b" * 64
        await bridge.refresh(force=True)
        with pytest.raises(NotificationPaused):
            await bridge.send_to(event, HA_DESTINATION)


async def test_docker_records_the_ha_phone_as_the_disruption_recipient(tmp_path, tg):
    def receiver(call):
        if call.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": VERSION,
                    "destination": HA_DESTINATION,
                    "wake_destination": None,
                    "wake_ready": False,
                },
            )
        assert call.headers["X-TCC-Destination"] == HA_DESTINATION
        return httpx.Response(200, json={"accepted": True})

    clock = TickClock(at("08:04"))
    provider = Provider(clock, every_day())
    provider.disruptions = ActiveDisruptions(
        (ActiveDisruption("incident", "Signal fault", frozenset({"UT"}), None),), "complete"
    )
    tg.other = receiver
    app = make_app(
        tmp_path,
        tg,
        clock,
        StationsProvider(provider, CODES),
        home_assistant_token=TOKEN,
        home_assistant_delivery=HaDeliverySettings(HA_ALERT_URL, frozenset({"p1"}), None),
    )
    try:
        await app.rt.start()
        state = app.rt.state.occurrences[f"p1|outbound|{DAY}"]
        assert set(state.disruptions["incident"].receipts) == {HA_DESTINATION}
        assert not tg.sent
    finally:
        await app.bot.close()
        await app.follower.close()


async def test_docker_uses_the_ha_arrival_identity_and_keeps_a_flexible_trip_on_telegram(
    tmp_path, tg
):
    deliveries = []
    available = True

    def receiver(call):
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        if call.method == "GET":
            if not available:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "version": VERSION,
                    "destination": HA_DESTINATION,
                    "wake_destination": None,
                    "wake_ready": False,
                },
            )
        event = decode(AlertEvent, json.loads(call.content)["event"])
        assert call.headers["X-TCC-Destination"] == HA_DESTINATION
        deliveries.append(event)
        return httpx.Response(200, json={"accepted": True})

    clock = TickClock(at("07:50"))
    tg.other = receiver
    app = make_app(
        tmp_path,
        tg,
        clock,
        home_assistant_token=TOKEN,
        home_assistant_delivery=HaDeliverySettings(HA_ALERT_URL, frozenset({"p1"}), None),
    )
    app.settings.profiles.write_text(
        app.settings.profiles.read_text() + "    alerts: {compensation_reminder: true}\n"
    )
    app.bot.state.confirmed_for = f"123:{CHAT}"
    try:
        await app.rt.start()
        change = {
            "action": "create",
            "plan": {
                "origin": "AAA",
                "destination": "UT",
                "start_date": (DAY + timedelta(days=4)).isoformat(),
                "outbound": {"mode": "exact", "departure": "08:12"},
            },
        }
        draft = app.rt.preview_travel_plan(change)
        saved = app.rt.save_travel_plan(change, draft["review_token"], "flex-delivery")
        flexible = app.rt.state.travel_plans[saved["plan_id"]].profile
        office = app.rt.config.profiles[0]
        telegram = app.bot.arrival_recipient
        assert telegram is not None
        assert app.rt._arrival_for(flexible.id) == telegram

        clock.t = at("10:05")
        key = OccurrenceKey(office.id, Direction.OUTBOUND, DAY)
        record = JourneyRecord(
            key,
            "RTD",
            PlannedJourney("RTD", at("08:12"), at("09:20"), "ns"),
            app.rt.config.revision,
        )
        record, obligation = app.rt._arrival_question(record, office, at("09:54"), clock.now())
        assert record.prompt.recipient == HA_DESTINATION
        assert obligation.target_ids == frozenset({HA_DESTINATION})
        assert app.rt._writer.commit_strict(
            replace(
                app.rt.state,
                obligations=(*app.rt.state.obligations, obligation),
                journey_records={**app.rt.state.journey_records, okey(key): record},
            )
        )
        await app.rt._deliver()
        (prompt,) = [event for event in deliveries if event.kind is EventKind.ARRIVAL_PROMPT]
        assert prompt.facts.prompt_id == record.prompt.id
        assert prompt.facts.choices == (("yes", "Yes"), ("other", "Other time"))
        assert app.rt.state.journey_records[okey(key)].prompt.delivered
        assert not tg.sent
        wrong = await app.rt.answer_arrival_prompt(record.prompt.id, "yes", telegram)
        assert isinstance(wrong, Refused)
        accepted = await app.rt.answer_arrival_prompt(record.prompt.id, "yes", HA_DESTINATION)
        assert accepted.arrived_at == at("09:54")

        available = False
        bridge = app.rt._routed_delivery
        assert isinstance(bridge, HaDelivery)
        await bridge.refresh(force=True)
        assert app.rt._arrival_for(office.id) is None
        with pytest.raises(NotificationPaused):
            await app.rt._send_to(prompt, HA_DESTINATION)
        assert not tg.sent
        assert await app.rt._send_to(
            replace(prompt, profile_id=flexible.id, kind=EventKind.NEXT_CONNECTION), telegram
        )
        assert tg.sent
    finally:
        await app.bot.close()
        await app.follower.close()


async def test_docker_wakes_the_selected_ha_phone_only_when_ready(tmp_path, tg):
    ready = False
    delivered = []

    def receiver(call):
        if call.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": VERSION,
                    "destination": HA_DESTINATION,
                    "wake_destination": HA_DESTINATION,
                    "wake_ready": ready,
                },
            )
        assert call.headers["X-TCC-Destination"] == HA_DESTINATION
        delivered.append(decode(AlertEvent, json.loads(call.content)["event"]))
        return httpx.Response(200, json={"accepted": True})

    clock = TickClock(at("06:18"))
    tg.other = receiver
    provider = Provider(
        clock,
        [
            make_connection([make_leg("AAA", "RTD", "08:12", "09:10", cancelled=True)]),
            make_connection([make_leg("AAA", "RTD", "07:42", "08:54", number="1111")]),
        ],
    )
    app = make_app(
        tmp_path,
        tg,
        clock,
        StationsProvider(provider, CODES),
        home_assistant_token=TOKEN,
        home_assistant_delivery=HaDeliverySettings(HA_ALERT_URL, frozenset({"p1"}), None),
    )
    path = app.settings.profiles
    path.write_text(
        path.read_text().replace("transfers: [UT]", "transfers: []", 1)
        + '    alerts: {wake_at: "06:50"}\n'
    )
    try:
        await app.rt.start()
        calls = len(provider.calls)
        await app.rt.wake_once()
        assert len(provider.calls) == calls
        assert not delivered

        ready = True
        bridge = app.rt._routed_delivery
        assert isinstance(bridge, HaDelivery)
        await bridge.refresh(force=True)
        await app.rt.wake_once()
        assert not delivered
        clock.t = at("06:20")
        await app.rt.wake_once()
        (event,) = delivered
        assert event.kind is EventKind.WAKE_ALARM
        assert event.profile_id == "p1"
        assert app.rt.state.wake_alarm_dates == {"p1": DAY}
        assert not tg.sent
    finally:
        await app.bot.close()
        await app.follower.close()


async def test_offline_migration_preserves_flexible_plans_and_older_journey_records(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, StationsProvider(Provider(clock, every_day()), CODES))
    await app.rt.start()
    profile = app.rt.view().profiles[0]
    plan = {
        "action": "create",
        "plan": {
            "origin": "AAA",
            "destination": "UT",
            "start_date": (DAY + timedelta(days=1)).isoformat(),
            "outbound": {"mode": "window", "arrive_by": "14:30"},
        },
    }
    draft = app.rt.preview_travel_plan(plan)
    if draft["status"] == "conflict":
        plan["conflict_policy"] = "replace"
        draft = app.rt.preview_travel_plan(plan)
    assert draft["status"] == "ready", draft
    saved = app.rt.save_travel_plan(plan, draft["review_token"], "day-trip")
    assert saved["status"] == "saved"
    key = OccurrenceKey(profile.id, Direction.OUTBOUND, DAY - timedelta(days=1))
    record = JourneyRecord(key, profile.work_station, None, route_revision=900)
    source = replace(app.rt.state, occurrences={}, journey_records={"old": record})
    docker = replace(
        app.rt.state, config=None, config_snapshots={}, occurrences={}, journey_records={}
    )
    merged = merge_profile_history(docker, source, profile.id, clock.now())
    assert merged.config == source.config
    assert merged.journey_records["old"] == record
    assert merged.travel_plans == docker.travel_plans
    assert merged.travel_receipts == docker.travel_receipts

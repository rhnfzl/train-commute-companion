import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from aiohttp.test_utils import TestServer
from ha_rig import PROFILES, setup_tcc, state_path, write_profiles
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_capture_events
from runtime_rig import MemoryStore, Notifier
from test_notifier import question
from test_services import attr, call, press

from custom_components.train_commute_companion import remote, remote_alert
from custom_components.train_commute_companion.const import EVENT_ALERT
from custom_components.train_commute_companion.host import THREAD_NAME
from custom_components.train_commute_companion.remote_alert import MAX_ALERT_BYTES, RemoteAlertView
from train_commute_companion.home_assistant_bridge import PATH, VERSION, dispatch
from train_commute_companion.model import EventKind
from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import encode

PROFILE = (PROFILES / "located_via_ut.yaml").read_text()
TOKEN = "a" * 64
ORIGIN = "https://docker.example.invalid"
HEALTH_ID = "sensor.office_a_health"
BUTTON_ID = "button.office_a_outbound_leaving"


@pytest.fixture
async def connected(hass, tcc_freezer, tmp_path, monkeypatch, socket_enabled):
    assert await async_setup_component(hass, "websocket_api", {})
    hass.states.async_set("zone.home", "0", {"latitude": 52.01, "longitude": 5.01, "radius": 100})
    hass.states.async_set("person.someone", "home", {"latitude": 52.01, "longitude": 5.01})
    h = await setup_tcc(hass, PROFILE, options={"notifications_enabled_entity": "switch.alerts"})
    registry = er.async_get(hass)
    identities = {entry_id: registry.async_get(entry_id) for entry_id in (HEALTH_ID, BUTTON_ID)}
    assert all(identities.values())
    state = state_path(hass, h.entry).read_bytes()

    path = tmp_path / "docker-profiles.yaml"
    path.write_text(PROFILE)
    runtime = Runtime(
        profile_path=path,
        store=MemoryStore(),
        provider=h.provider,
        notifier=Notifier(),
        clock=h.clock,
        require_stations=True,
    )
    await runtime.start()
    wire = SimpleNamespace(messages=[], status=200, hide_profiles=False)

    async def bridge(req):
        assert str(req.url) == ORIGIN + PATH
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        message = json.loads(req.content)
        wire.messages.append(message)
        if wire.status != 200:
            return httpx.Response(wire.status)
        if wire.hide_profiles and message["method"] == "view":
            empty = replace(runtime.view(), profiles=(), directions=(), occurrences=())
            return httpx.Response(
                200, json={"version": VERSION, "kind": "RuntimeView", "result": encode(empty)}
            )
        return httpx.Response(200, json=await dispatch(runtime, message))

    monkeypatch.setattr(
        remote,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **options: httpx.AsyncClient(
                transport=httpx.MockTransport(bridge), **options
            ),
            HTTPError=httpx.HTTPError,
        ),
    )
    options = hass.config_entries.options
    flow = await options.async_init(h.entry.entry_id)
    flow = await options.async_configure(flow["flow_id"], {"next_step_id": "runtime_connection"})
    invalid = await options.async_configure(
        flow["flow_id"],
        {
            "remote_url": ORIGIN.replace("https", "http"),
            "remote_token": TOKEN,
        },
    )
    assert invalid["errors"] == {"base": "remote_settings_invalid"}
    assert h.entry.state is ConfigEntryState.LOADED and h.host.alive and not wire.messages
    saved = await options.async_configure(
        flow["flow_id"], {"remote_url": ORIGIN, "remote_token": TOKEN}
    )
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert h.entry.state is ConfigEntryState.LOADED and not h.host.alive
    assert all(thread.name != THREAD_NAME for thread in threading.enumerate())
    assert isinstance(h.entry.runtime_data.host, remote.RemoteRuntime)
    assert attr(hass, HEALTH_ID, "runtime_source") == "docker"
    assert all(registry.async_get(entity_id) == old for entity_id, old in identities.items())
    menu = await options.async_init(h.entry.entry_id)
    assert set(menu["menu_options"]) == {"settings", "recovery", "arrival", "runtime_connection"}
    return h, runtime, wire, state


async def test_connected_entry_routes_existing_services_buttons_and_profile_location(
    hass, connected
):
    h, runtime, wire, local_state = connected
    await press(hass, BUTTON_ID)
    await call(
        hass, "not_travelling", profile="p1", direction="outbound", service_date="2026-10-27"
    )
    await call(hass, "travelling", profile="p1", direction="outbound", service_date="2026-10-27")
    await call(hass, "choose", profile="p1", direction="outbound", departure="08:44")
    assert attr(hass, "sensor.office_a_outbound_lock", "source") == "choose"
    with pytest.raises(ServiceValidationError, match="this question has expired"):
        await call(hass, "answer_prompt", prompt_id="0123456789", answer="yes")
    with pytest.raises(ServiceValidationError, match="Enable schedule synchronization"):
        await call(hass, "reload_profiles")
    await call(
        hass,
        "sync_schedule",
        profile="p1",
        extra_dates=["2026-11-01"],
        outbound_buffer_min=12,
        return_buffer_min=7,
    )
    assert next(p for p in runtime.view().profiles if p.id == "p1").outbound.buffer_min == 12
    write_profiles(hass, PROFILE.replace("buffer_min: 10", "buffer_min: 13"))
    hass.config_entries.async_update_entry(
        h.entry, options={**h.entry.options, "remote_profile_sync": True}
    )
    await call(hass, "reload_profiles")
    assert next(p for p in runtime.view().profiles if p.id == "p1").outbound.buffer_min == 13
    hass.states.async_set("person.someone", "not_home", {"latitude": 52.05, "longitude": 5.05})
    await hass.async_block_till_done()
    names = [message["method"] for message in wire.messages if message["method"] != "view"]
    assert names == [
        "leaving",
        "not_travelling",
        "travelling",
        "choose_at",
        "answer_prompt",
        "sync_profile",
        "sync_profile",
        "report_location",
    ]
    location = wire.messages[-1]["arguments"]
    assert location["profile_id"] == "p1" and location["source"]["entity"] == "person.someone"
    assert location["event"]["kind"] == "leave"
    assert state_path(hass, h.entry).read_bytes() == local_state

    registry = er.async_get(hass)
    before = registry.async_get(HEALTH_ID)
    wire.status = 503
    await h.entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(HEALTH_ID).state == "unavailable"
    assert registry.async_get(HEALTH_ID) == before
    wire.status = 200
    await h.entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(HEALTH_ID).state != "unavailable"
    assert registry.async_get(HEALTH_ID) == before
    wire.hide_profiles = True
    await h.entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(HEALTH_ID).state == "unavailable"
    assert registry.async_get(HEALTH_ID) == before


@pytest.fixture
async def bridge_alert_client(hass, connected, aiohttp_client):
    server = TestServer(hass.http.app)
    await server.start_server(access_log=None)
    client = await aiohttp_client(server)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    reply = await client.get(RemoteAlertView.url, headers=headers)
    target = (await reply.json())["destination"]
    assert target == connected[0].entry.runtime_data.notifier.destination_id
    return client, {**headers, "X-TCC-Destination": target}


async def test_bridge_alert_uses_ha_phone_gate_event_and_button(
    hass, connected, bridge_alert_client
):
    h, _runtime, wire, _state = connected
    client, headers = bridge_alert_client
    url = RemoteAlertView.url
    body = {"version": VERSION, "event": encode(question())}
    events = async_capture_events(hass, EVENT_ALERT)
    assert (await client.post(url, json=body)).status == 401
    assert (
        await client.post(url, headers=headers, data=b"x" * (MAX_ALERT_BYTES + 1))
    ).status == 413
    assert (
        await client.post(url, headers={"Authorization": headers["Authorization"]}, json=body)
    ).status == 412
    wrong_target = {**headers, "X-TCC-Destination": "someone-else"}
    assert (await client.post(url, headers=wrong_target, json=body)).status == 412
    wake = {"version": VERSION, "event": encode(replace(question(), kind=EventKind.WAKE_ALARM))}
    assert (await client.post(url, headers=headers, json=wake)).status == 412

    hass.states.async_set("switch.alerts", "off")
    before = len(h.pushes)
    reply = await client.post(url, headers=headers, json=body)
    assert await reply.json() == {"accepted": False, "paused": True}
    assert not events and len(h.pushes) == before

    hass.states.async_set("switch.alerts", "on")
    reply = await client.post(url, headers=headers, json=body)
    assert await reply.json() == {"accepted": True, "paused": False}
    assert len(events) == 1 and events[0].data["config_entry_id"] == h.entry.entry_id
    action = h.pushes[-1].data["data"]["actions"][0]["action"]
    before = len(wire.messages)
    hass.bus.async_fire("mobile_app_notification_action", {"action": action})
    await hass.async_block_till_done()
    assert [
        message["arguments"]
        for message in wire.messages[before:]
        if message["method"] == "answer_prompt"
    ] == [{"prompt_id": "0123456789", "answer": "yes"}]


async def test_bridge_alert_retry_shares_one_send_and_uses_stored_receipt_after_reload(
    hass, connected, bridge_alert_client, monkeypatch
):
    h, _runtime, _wire, _state = connected
    client, headers = bridge_alert_client
    event = question()
    body = {"version": VERSION, "event": encode(event)}
    events = async_capture_events(hass, EVENT_ALERT)
    original_send = h.entry.runtime_data.notifier.send
    push_count = len(h.pushes)
    original_authorized = RemoteAlertView._authorized
    sends = []
    matching_requests = 0
    entered = asyncio.Event()
    concurrent = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(event):
        sends.append(event)
        entered.set()
        await release.wait()
        return await original_send(event)

    def observe_auth(view, request):
        nonlocal matching_requests
        matching_requests += 1
        if matching_requests == 2:
            concurrent.set()
        return original_authorized(view, request)

    monkeypatch.setattr(h.entry.runtime_data.notifier, "send", slow_send)
    monkeypatch.setattr(RemoteAlertView, "_authorized", observe_auth)

    async def send():
        reply = await client.post(RemoteAlertView.url, headers=headers, json=body)
        return await reply.json()

    async with asyncio.TaskGroup() as group:
        first = group.create_task(send())
        await entered.wait()
        second = group.create_task(send())
        await concurrent.wait()
        release.set()
    assert first.result() == second.result() == {"accepted": True, "paused": False}
    assert len(sends) == len(events) == len(h.pushes) - push_count == 1

    assert await hass.config_entries.async_reload(h.entry.entry_id)
    await hass.async_block_till_done()
    assert h.entry.runtime_data.notifier.send is not slow_send
    assert await send() == {"accepted": True, "paused": False}
    assert len(events) == len(h.pushes) - push_count == 1


async def test_bridge_alert_does_not_acknowledge_a_failed_receipt_write(
    hass, connected, bridge_alert_client, monkeypatch
):
    h, _runtime, _wire, _state = connected
    client, headers = bridge_alert_client
    body = {"version": VERSION, "event": encode(question())}
    events = async_capture_events(hass, EVENT_ALERT)
    push_count = len(h.pushes)

    async def send():
        reply = await client.post(RemoteAlertView.url, headers=headers, json=body)
        return await reply.json()

    # Store can return normally after logging a failed write.
    async def silent_failure(self, data):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(remote_alert.Store, "async_save", silent_failure)
        assert await send() == {"accepted": False, "paused": False}
    assert len(events) == len(h.pushes) - push_count == 1
    assert await send() == {"accepted": True, "paused": False}
    assert len(events) == len(h.pushes) - push_count == 1

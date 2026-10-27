import asyncio
import logging
from datetime import date, datetime

import pytest
from aiohttp import ClientError
from harness import fake_phone
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from custom_components.train_commute_companion import notifier as notifier_mod
from custom_components.train_commute_companion.const import (
    DEFAULT_LOUD_STYLE,
    EVENT_ALERT,
    LOUD_STYLES,
    NOTIFY_BACKOFF_S,
)
from custom_components.train_commute_companion.notifier import HaNotifier, notify_payload
from train_commute_companion.calendar import TZ
from train_commute_companion.model import (
    AlertEvent,
    Direction,
    EventKind,
    Severity,
    TrainId,
)

EXPECTED_KEYS = {
    "config_entry_id", "device_id", "profile_id", "profile_name", "direction", "service_date",
    "leg_index", "kind", "severity", "train_operating_date", "train_number", "train_departure",
    "text", "key", "created_at",
}  # fmt: skip
RELAY = "https://relay.example/push"


def make_event(severity=Severity.LOUD, created_at=None, key="k1") -> AlertEvent:
    dep = datetime(2026, 10, 27, 8, 12, tzinfo=TZ)
    return AlertEvent(
        profile_id="p1",
        profile_name="Commute",
        direction=Direction.OUTBOUND,
        service_date=date(2026, 10, 27),
        leg_index=0,
        kind=EventKind.CANCELLED,
        severity=severity,
        train=TrainId(date(2026, 10, 27), "1234", dep),
        text="Your 08:12 is cancelled.",
        key=key,
        created_at=created_at or datetime(2026, 10, 27, 7, 50, tzinfo=TZ),
    )


def loud_event() -> AlertEvent:
    return make_event()


def build(hass, **kw) -> HaNotifier:
    return HaNotifier(hass, "entry1", "mobile_app_phone", **kw)


async def send_from_thread(hass, event, notifier=None, **kw) -> bool:
    n = notifier or build(hass, **kw)
    return await hass.async_add_executor_job(asyncio.run, n.send(event))


def test_loud_styles_constants():
    assert LOUD_STYLES == ("time_sensitive", "critical")
    assert DEFAULT_LOUD_STYLE == "time_sensitive"


async def test_loud_payload_default_is_time_sensitive(hass, monkeypatch):
    calls = fake_phone(hass, monkeypatch)
    assert await send_from_thread(hass, loud_event())
    data = calls[0].data["data"]
    assert data["push"]["interruption-level"] == "time-sensitive" and "critical" not in str(data)
    assert (data["ttl"], data["priority"], data["channel"]) == (0, "high", "Train alerts")
    assert calls[0].data["message"] == loud_event().text


async def test_loud_payload_critical_when_chosen(hass, monkeypatch):
    calls = fake_phone(hass, monkeypatch)
    assert await send_from_thread(hass, loud_event(), loud_style="critical")
    data = calls[0].data["data"]
    assert (
        data["push"]["interruption-level"] == "critical" and data["push"]["sound"]["critical"] == 1
    )
    assert (data["ttl"], data["priority"], data["channel"]) == (0, "high", "alarm_stream")


def test_unknown_loud_style_raises():
    with pytest.raises(ValueError):
        notify_payload(Severity.LOUD, "x", "siren")


async def test_silent_payload_has_no_sound(hass, monkeypatch):
    calls = fake_phone(hass, monkeypatch)
    assert await send_from_thread(hass, make_event(Severity.SILENT))
    assert calls[0].data["data"] == {
        "push": {"interruption-level": "passive"},
        "channel": "Train updates",
        "importance": "low",
    }
    assert "sound" not in str(calls[0].data)


async def test_non_phone_target_is_not_called_and_not_delivered(hass):
    calls = async_mock_service(hass, "notify", "mobile_app_fake")  # user-made, no mobile_app entry
    payload = notify_payload(Severity.LOUD, "x")
    assert await notifier_mod._push(hass, "mobile_app_fake", payload) == (False, False)
    assert calls == []


async def test_missing_service_returns_false(hass):
    assert not await send_from_thread(hass, loud_event())


async def test_raising_service_returns_false(hass, monkeypatch):
    async def boom(call):
        raise HomeAssistantError("nope")

    fake_phone(hass, monkeypatch, handler=boom)
    assert not await send_from_thread(hass, loud_event())


async def test_hanging_service_times_out_false(hass, monkeypatch):
    monkeypatch.setattr(notifier_mod, "NOTIFY_TIMEOUT_S", 0.05)

    async def hang(call):
        await asyncio.sleep(30)

    fake_phone(hass, monkeypatch, handler=hang)
    assert not await send_from_thread(hass, loud_event())


async def test_after_a_timeout_next_send_fails_fast_then_recovers(hass, monkeypatch):
    monkeypatch.setattr(notifier_mod, "NOTIFY_TIMEOUT_S", 0.05)
    hang = True
    calls = []

    async def svc(call):
        calls.append(call)
        if hang:
            await asyncio.sleep(30)

    fake_phone(hass, monkeypatch, handler=svc)
    n = build(hass)
    assert not await send_from_thread(hass, make_event(key="a"), n)
    assert not await send_from_thread(hass, make_event(key="b"), n)
    assert len(calls) == 1  # the second failed fast, no call
    hang = False
    now = notifier_mod.time.monotonic()
    monkeypatch.setattr(notifier_mod.time, "monotonic", lambda: now + NOTIFY_BACKOFF_S + 1)
    assert await send_from_thread(hass, make_event(key="b"), n)
    assert len(calls) == 2


# real mobile_app path
PHONE = {
    "manufacturer": "Google",
    "model": "Pixel",
    "os_name": "Android",
    "os_version": "16",
    "app_id": "io.homeassistant.companion.android",
    "app_name": "Companion",
    "app_version": "1.0",
    "supports_encryption": False,
}


@pytest.fixture
async def phone(hass, hass_admin_user):
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "mobile_app", {})
    entry = MockConfigEntry(
        domain="mobile_app",
        data={
            **PHONE,
            "webhook_id": "hook1",
            "device_id": "dev1",
            "device_name": "Phone",
            "user_id": hass_admin_user.id,
            "app_data": {"push_url": RELAY, "push_token": "tok"},
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service("notify", "mobile_app_phone")
    return entry


@pytest.mark.parametrize("relay", [500, 429, "client_error", "timeout"])
async def test_relay_failure_logged_by_mobile_app_is_false(hass, aioclient_mock, phone, relay):
    if relay == "client_error":
        aioclient_mock.post(RELAY, exc=ClientError())
    elif relay == "timeout":
        aioclient_mock.post(RELAY, exc=TimeoutError())
    else:
        aioclient_mock.post(RELAY, status=relay, json={})
    assert not await send_from_thread(hass, loud_event())
    assert aioclient_mock.call_count == 1  # the service returned normally, HA only logged


async def test_relay_accepts_is_true(hass, aioclient_mock, phone):
    aioclient_mock.post(RELAY, status=201, json={})
    assert await send_from_thread(hass, loud_event())
    assert aioclient_mock.mock_calls[0][2]["message"] == loud_event().text


async def test_real_relay_timeout_backs_off(hass, aioclient_mock, phone):
    # mobile_app turns the relay's TimeoutError into HomeAssistantError (rule 12)
    aioclient_mock.post(RELAY, exc=TimeoutError())
    n = build(hass)
    assert not await send_from_thread(hass, make_event(key="a"), n)
    assert not await send_from_thread(hass, make_event(key="b"), n)
    assert aioclient_mock.call_count == 1  # the second failed fast, no relay call


async def open_channel(hass, hass_ws_client, confirm=True):
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {
            "id": 5,
            "type": "mobile_app/push_notification_channel",
            "webhook_id": "hook1",
            "support_confirm": confirm,
        }
    )
    assert (await ws.receive_json())["success"]
    assert notifier_mod.mobile_target(hass, "mobile_app_phone") == "hook1"
    return ws


async def test_local_push_unconfirmed_and_fallback_failing_is_false(
    hass, hass_ws_client, aioclient_mock, phone, monkeypatch
):
    monkeypatch.setattr(
        "homeassistant.components.mobile_app.push_notification.PUSH_CONFIRM_TIMEOUT", 0.05
    )
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 0.5)
    aioclient_mock.post(RELAY, status=500, json={})
    await open_channel(hass, hass_ws_client)
    assert not await send_from_thread(hass, loud_event())
    assert aioclient_mock.call_count == 1  # the fallback ran inside the settle window


async def test_local_push_confirmed_is_true(hass, hass_ws_client, phone, monkeypatch):
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 0.3)
    ws = await open_channel(hass, hass_ws_client)
    task = hass.async_create_task(send_from_thread(hass, loud_event()))
    msg = await ws.receive_json()
    confirm_id = msg["event"]["hass_confirm_id"]
    await ws.send_json(
        {
            "id": 6,
            "type": "mobile_app/push_notification_confirm",
            "webhook_id": "hook1",
            "confirm_id": confirm_id,
        }
    )
    assert (await ws.receive_json())["success"]
    assert await task


@pytest.mark.parametrize(("relay", "delivered"), [(201, True), (500, False)])
async def test_local_push_without_confirmation_counts_only_the_relay_answer(
    hass, hass_ws_client, aioclient_mock, phone, relay, delivered
):  # 7.12 rules 5, 16: HA drops a websocket push on a closing socket, so it proves nothing
    aioclient_mock.post(RELAY, status=relay, json={})
    ws = await open_channel(hass, hass_ws_client, confirm=False)
    assert await send_from_thread(hass, loud_event()) is delivered
    assert aioclient_mock.call_count == 1
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await ws.receive_json()  # nothing went over the unconfirmed channel


async def test_local_push_without_confirmation_or_relay_is_false(
    hass, hass_ws_client, hass_admin_user
):
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "mobile_app", {})
    entry = MockConfigEntry(domain="mobile_app", data={**PHONE, "webhook_id": "hook1",
        "device_id": "dev1", "device_name": "Phone", "user_id": hass_admin_user.id,
        "app_data": {"push_websocket_channel": True}})  # fmt: skip
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await open_channel(hass, hass_ws_client, confirm=False)
    assert not await send_from_thread(hass, loud_event())


def test_no_mobile_target_without_mobile_app(hass):
    assert notifier_mod.mobile_target(hass, "mobile_app_phone") is None


async def test_local_push_unconfirmed_and_fallback_succeeding_is_true(
    hass, hass_ws_client, aioclient_mock, phone, monkeypatch
):
    monkeypatch.setattr(
        "homeassistant.components.mobile_app.push_notification.PUSH_CONFIRM_TIMEOUT", 0.05
    )
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 5)
    aioclient_mock.post(RELAY, status=201, json={})
    await open_channel(hass, hass_ws_client)
    assert await send_from_thread(hass, loud_event())
    assert aioclient_mock.call_count == 1


async def test_late_confirm_timer_is_unproven_and_false(
    hass, hass_ws_client, aioclient_mock, phone, monkeypatch
):
    # a busy hass.loop starts HA's confirm timeout late: at our deadline no confirmation came and
    # the fallback has not even run, so the push is not proven
    monkeypatch.setattr(
        "homeassistant.components.mobile_app.push_notification.PUSH_CONFIRM_TIMEOUT", 30
    )
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 0.2)
    aioclient_mock.post(RELAY, status=500, json={})
    await open_channel(hass, hass_ws_client)
    assert not await send_from_thread(hass, loud_event())
    assert aioclient_mock.call_count == 0
    channel = hass.data["mobile_app"]["push_channel"]["hook1"]
    for pending in channel.pending_confirms.values():
        pending["unsub_scheduled_push_failed"]()  # end HA's late timer, so none lingers


async def test_local_push_unconfirmed_at_the_deadline_backs_off(
    hass, hass_ws_client, aioclient_mock, phone, monkeypatch
):
    monkeypatch.setattr(
        "homeassistant.components.mobile_app.push_notification.PUSH_CONFIRM_TIMEOUT", 30
    )
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 0.2)
    await open_channel(hass, hass_ws_client)
    channel = hass.data["mobile_app"]["push_channel"]["hook1"]
    sent = []
    real = channel.async_send_notification
    monkeypatch.setattr(
        channel, "async_send_notification", lambda m, f: (sent.append(m), real(m, f))
    )
    n = build(hass)
    assert not await send_from_thread(hass, make_event(key="a"), n)
    assert not await send_from_thread(hass, make_event(key="b"), n)
    assert len(sent) == 1  # the second waited out no second deadline
    for pending in channel.pending_confirms.values():
        pending["unsub_scheduled_push_failed"]()


@pytest.mark.parametrize("level", [logging.ERROR, logging.CRITICAL])
async def test_relay_rejection_is_false_whatever_the_log_level(hass, aioclient_mock, phone, level):
    logger = logging.getLogger("homeassistant.components.mobile_app")
    old = logger.level
    logger.setLevel(level)
    try:
        aioclient_mock.post(RELAY, status=500, json={})
        assert not await send_from_thread(hass, loud_event())
        assert aioclient_mock.call_count == 1
    finally:
        logger.setLevel(old)


async def test_colliding_phone_names_use_the_phone_ha_targets(
    hass, hass_ws_client, hass_admin_user, aioclient_mock, monkeypatch
):
    """'Phone A' and 'Phone_A' share the slug; HA keeps the last one. Only that phone has a
    confirming channel: the push goes unconfirmed and its relay fallback refuses."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "mobile_app", {})
    for hook, name, url in (("hookA", "Phone A", RELAY + "/a"), ("hookB", "Phone_A", RELAY + "/b")):
        entry = MockConfigEntry(domain="mobile_app", data={**PHONE, "webhook_id": hook,
            "device_id": hook, "device_name": name, "user_id": hass_admin_user.id,
            "app_data": {"push_url": url, "push_token": hook}})  # fmt: skip
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert notifier_mod.mobile_target(hass, "mobile_app_phone_a") == "hookB"
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 5, "type": "mobile_app/push_notification_channel",
        "webhook_id": "hookB", "support_confirm": True})  # fmt: skip
    assert (await ws.receive_json())["success"]
    monkeypatch.setattr(
        "homeassistant.components.mobile_app.push_notification.PUSH_CONFIRM_TIMEOUT", 0.05
    )
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 5)
    aioclient_mock.post(RELAY + "/b", status=500, json={})
    n = HaNotifier(hass, "entry1", "mobile_app_phone_a")
    assert not await send_from_thread(hass, loud_event(), n)
    assert [str(c[1]) for c in aioclient_mock.mock_calls] == [RELAY + "/b"]


async def pending_local_push(hass, hass_ws_client, phone, monkeypatch):
    """A send whose local push went out and waits for a confirmation that never comes."""
    monkeypatch.setattr(
        "homeassistant.components.mobile_app.push_notification.PUSH_CONFIRM_TIMEOUT", 0.05
    )
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 5)
    ws = await open_channel(hass, hass_ws_client)
    n = build(hass)
    task = hass.async_create_task(n._send(loud_event()))
    await ws.receive_json()  # the push reached the phone's websocket
    return n, task


async def test_retired_notifier_never_relays_from_the_fallback(
    hass, hass_ws_client, aioclient_mock, phone, monkeypatch
):
    aioclient_mock.post(RELAY, status=201, json={})
    n, task = await pending_local_push(hass, hass_ws_client, phone, monkeypatch)
    n.retired.set()
    assert await task is False  # HA's confirm timer ran the fallback, which saw the retirement
    assert aioclient_mock.call_count == 0


async def test_stopped_notifier_withdraws_its_pending_push(
    hass, hass_ws_client, aioclient_mock, phone, monkeypatch
):
    aioclient_mock.post(RELAY, status=201, json={})
    n, task = await pending_local_push(hass, hass_ws_client, phone, monkeypatch)
    n.retired.set()
    await n.async_stop()
    assert task.cancelled()
    channel = hass.data["mobile_app"]["push_channel"]["hook1"]
    assert channel.pending_confirms == {}  # HA's confirm timer is gone with it
    await asyncio.sleep(0.2)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0


async def test_stopped_notifier_cancels_a_running_fallback(
    hass, hass_ws_client, aioclient_mock, phone, monkeypatch
):
    started = asyncio.Event()

    async def slow_relay(*_):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr("homeassistant.components.mobile_app.notify._send_message", slow_relay)
    n, task = await pending_local_push(hass, hass_ws_client, phone, monkeypatch)
    await asyncio.wait_for(started.wait(), 2)
    n.retired.set()
    await n.async_stop()
    assert task.cancelled() and n._tasks == set()


async def test_retired_notifier_neither_fires_nor_pushes(hass, monkeypatch):
    calls = fake_phone(hass, monkeypatch)
    events = async_capture_events(hass, EVENT_ALERT)
    n = build(hass)
    n.retired.set()
    assert not await send_from_thread(hass, loud_event(), n)
    assert not await n._send(loud_event())
    assert calls == [] and events == []


async def test_bus_event_fired_once_per_fact_with_all_fields(hass):
    events = async_capture_events(hass, EVENT_ALERT)
    n = build(hass)
    e = loud_event()
    await send_from_thread(hass, e, n)
    await send_from_thread(hass, e, n)  # a retry of the same fact
    assert len(events) == 1 and set(events[0].data) == EXPECTED_KEYS
    d = events[0].data
    assert d["severity"] == "loud" and d["direction"] == "outbound" and d["device_id"] is None
    assert d["config_entry_id"] == "entry1" and d["train_number"] == "1234"
    assert d["service_date"] == "2026-10-27" and d["created_at"].startswith("2026-10-27T07:50")


async def test_bus_event_has_device_id_when_registered(hass):
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(domain="train_commute_companion")
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("train_commute_companion", "p1")}
    )
    events = async_capture_events(hass, EVENT_ALERT)
    await send_from_thread(hass, loud_event())
    assert events[0].data["device_id"] == dev.id


async def test_recurrence_with_new_created_at_fires_again(hass):
    events = async_capture_events(hass, EVENT_ALERT)
    n = build(hass)
    await send_from_thread(hass, loud_event(), n)
    await send_from_thread(hass, make_event(created_at=datetime(2026, 10, 27, 8, 5, tzinfo=TZ)), n)
    assert len(events) == 2


async def test_bus_event_fires_even_when_push_fails(hass):
    events = async_capture_events(hass, EVENT_ALERT)
    assert not await send_from_thread(hass, loud_event())  # no notify service
    assert len(events) == 1


async def test_runtime_side_timeout_when_hass_never_answers(hass, monkeypatch):
    monkeypatch.setattr(notifier_mod, "NOTIFY_TIMEOUT_S", 0.02)
    monkeypatch.setattr(notifier_mod, "LOCAL_PUSH_SETTLE_S", 0.02)
    n = build(hass)

    async def never(event):
        await asyncio.Event().wait()

    monkeypatch.setattr(n, "_send", never)
    assert not await send_from_thread(hass, loud_event(), n)

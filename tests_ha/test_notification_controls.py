"""Notification controls preserve current warnings without claiming a paused delivery."""

import asyncio
from dataclasses import replace

import pytest
from builders import make_leg, only_occurrence
from ha_rig import PROFILE, drive, fake_phone, jump, setup_tcc
from homeassistant.data_entry_flow import InvalidData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_capture_events
from runtime_rig import escape_world
from test_config_flow import add_entry, current, settings
from test_notifier import make_event

from custom_components.train_commute_companion.const import DOMAIN, EVENT_ALERT
from custom_components.train_commute_companion.diagnostics import async_get_config_entry_diagnostics
from custom_components.train_commute_companion.entity import health_state
from custom_components.train_commute_companion.notifier import HaNotifier
from train_commute_companion.model import Direction, EventKind

ENABLED = "input_boolean.notifications_enabled"
QUIET = "binary_sensor.notifications_quiet"
OPTIONS = {"notifications_enabled_entity": ENABLED, "notifications_quiet_entity": QUIET}


def cancel_train(h):
    first = h.provider.world[0]
    gone = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True)
    h.provider.world[0] = replace(first, legs=(gone, *first.legs[1:]))


@pytest.mark.usefixtures("tcc_freezer")
async def test_paused_runtime_retains_current_warning_without_failure_or_receipt(hass):
    hass.states.async_set(ENABLED, "off")
    events = async_capture_events(hass, EVENT_ALERT)
    h = await setup_tcc(hass, PROFILE, options=OPTIONS)
    cancel_train(h)
    await drive(h, "08:06")
    assert not h.pushes and not events
    pending = await h.host.run(lambda rt: rt.state.obligations)
    assert any(o.event.kind is EventKind.CANCELLED for o in pending)
    assert all(o.attempts == 0 for o in pending)
    assert await h.host.run(lambda rt: rt.events_log) == []
    assert h.entry.runtime_data.coordinator.data.undelivered == 0
    assert hass.states.get("sensor.office_a_health").state == "notifications_paused"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing") is None
    data = await async_get_config_entry_diagnostics(hass, h.entry)
    assert data["runtime"]["notifications_paused"] is True
    assert data["runtime"]["notifications_quiet"] is False

    hass.states.async_set(ENABLED, "on")
    await drive(h, "08:07")
    cancelled = [e for e in events if e.data["kind"] == "cancelled"]
    assert len(cancelled) == 1
    assert any("cancelled" in c.data["message"] for c in h.pushes)
    assert not any(
        o.event.kind is EventKind.CANCELLED
        for o in await h.host.run(lambda rt: rt.state.obligations)
    )


@pytest.mark.usefixtures("tcc_freezer")
async def test_unpause_drops_delay_when_train_recovers(hass):
    hass.states.async_set(ENABLED, "off")
    events = async_capture_events(hass, EVENT_ALERT)
    h = await setup_tcc(hass, PROFILE, options=OPTIONS)
    original = h.provider.world[0]
    delayed = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=10, delay_arr=10)
    h.provider.world[0] = replace(original, legs=(delayed, *original.legs[1:]))
    await drive(h, "08:06")
    pending = await h.host.run(lambda rt: rt.state.obligations)
    assert any(
        o.event.kind is EventKind.DELAYED and o.event.train.train_number == "1234" for o in pending
    )
    h.provider.world[0] = original
    await drive(h, "08:08")
    hass.states.async_set(ENABLED, "on")
    await drive(h, "08:09")
    assert not any(e.data["kind"] == "delayed" and e.data["train_number"] == "1234" for e in events)


@pytest.mark.usefixtures("tcc_freezer")
async def test_paused_advice_is_not_told_and_expired_advice_never_emits(hass):
    hass.states.async_set(ENABLED, "off")
    events = async_capture_events(hass, EVENT_ALERT)
    h = await setup_tcc(hass, PROFILE, now="16:54", world=escape_world(), options=OPTIONS)
    pending = await h.host.run(lambda rt: rt.state.obligations)
    advice = next(o for o in pending if o.event.kind is EventKind.LEAVE_NOW)
    record = await h.host.run(lambda rt: only_occurrence(rt, Direction.RETURN).advice)
    assert not record.delivered
    assert advice.attempts == 0 and not h.pushes and not events
    await jump(h, "2026-10-27 17:23")
    await h.host.run(lambda rt: rt.watchdog_once())
    hass.states.async_set(ENABLED, "on")
    await h.host.run(lambda rt: rt.watchdog_once())
    assert not any(e.data["kind"] == "leave_now" for e in events)
    assert not any(o.id == advice.id for o in await h.host.run(lambda rt: rt.state.obligations))


@pytest.mark.usefixtures("tcc_freezer")
async def test_pause_preserves_existing_delivery_failure(hass):
    hass.states.async_set(ENABLED, "on")
    h = await setup_tcc(hass, PROFILE, options=OPTIONS)

    async def fail(call):
        raise HomeAssistantError("relay refused")

    hass.services.async_register("notify", "mobile_app_phone", fail)
    cancel_train(h)
    await drive(h, "08:06")
    assert h.entry.runtime_data.coordinator.data.undelivered > 0
    hass.states.async_set(ENABLED, "off")
    await drive(h, "08:07")
    assert hass.states.get("sensor.office_a_health").state == "notify_failing"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "notify_failing") is not None


@pytest.mark.usefixtures("tcc_freezer")
async def test_faults_take_priority_over_paused_health(hass):
    hass.states.async_set(ENABLED, "off")
    h = await setup_tcc(hass, PROFILE, options=OPTIONS)
    view = h.entry.runtime_data.coordinator.data
    for changes, expected in (
        ({"key_rejected": True}, "key_rejected"),
        ({"store_ok": False}, "storage_failed"),
        ({"refusal": "invalid profile"}, "profile_refused"),
        ({"undelivered": 1}, "notify_failing"),
    ):
        assert health_state(replace(view, **changes), "p1", notifications_paused=True) == expected


@pytest.mark.parametrize("state", ["on", "unknown", "unavailable", None])
async def test_missing_or_unknown_pause_control_does_not_hide_warnings(hass, monkeypatch, state):
    calls = fake_phone(hass, monkeypatch)
    if state is not None:
        hass.states.async_set(ENABLED, state)
    notifier = HaNotifier(hass, "entry1", "mobile_app_phone", enabled_entity=ENABLED)
    assert await hass.async_add_executor_job(asyncio.run, notifier.send(make_event()))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "state,quiet",
    [("on", True), ("off", False), ("unknown", False), ("unavailable", False), (None, False)],
)
async def test_quiet_changes_phone_sound_without_changing_warning_identity(
    hass, monkeypatch, state, quiet
):
    calls = fake_phone(hass, monkeypatch)
    events = async_capture_events(hass, EVENT_ALERT)
    if state is not None:
        hass.states.async_set(QUIET, state)
    notifier = HaNotifier(hass, "entry1", "mobile_app_phone", quiet_entity=QUIET)
    assert await hass.async_add_executor_job(asyncio.run, notifier.send(make_event()))
    data = calls[0].data["data"]
    assert data["push"]["interruption-level"] == ("passive" if quiet else "time-sensitive")
    assert ("sound" not in data["push"]) is quiet
    assert events[0].data["severity"] == "loud"


async def test_quiet_critical_retry_keeps_same_tag_and_event(hass, monkeypatch):
    calls = fake_phone(hass, monkeypatch)
    events = async_capture_events(hass, EVENT_ALERT)
    hass.states.async_set(QUIET, "on")
    notifier = HaNotifier(hass, "entry1", "mobile_app_phone", "critical", quiet_entity=QUIET)
    event = make_event()
    assert await hass.async_add_executor_job(asyncio.run, notifier.send(event))
    hass.states.async_set(QUIET, "off")
    assert await hass.async_add_executor_job(asyncio.run, notifier.send(event))
    quiet, critical = [call.data["data"] for call in calls]
    assert "sound" not in quiet["push"]
    assert critical["push"]["sound"]["critical"] == 1
    assert quiet["tag"] == critical["tag"]
    assert len(events) == 1 and events[0].data["severity"] == "loud"


async def test_options_save_and_clear_optional_notification_entities(hass, monkeypatch):
    calls = fake_phone(hass, monkeypatch)
    entry = add_entry(hass)
    result = await settings(hass, entry, **current(entry, **OPTIONS))
    assert result["type"] == "create_entry"
    assert {k: entry.options[k] for k in OPTIONS} == OPTIONS
    assert not calls
    result = await settings(hass, entry, **current(entry))
    assert result["type"] == "create_entry"
    assert not any(k in entry.options for k in OPTIONS)


async def test_options_reject_invalid_notification_entity(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    entry = add_entry(hass)
    with pytest.raises(InvalidData):
        await settings(hass, entry, **current(entry, notifications_enabled_entity="not-an-entity"))
    assert "notifications_enabled_entity" not in entry.options

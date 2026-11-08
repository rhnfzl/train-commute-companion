from ha_rig import fake_phone
from homeassistant.data_entry_flow import FlowResultType

from custom_components.train_commute_companion.config_flow import TccConfigFlow


async def test_setup_cannot_commit_before_notification_proof(hass, monkeypatch):
    fake_phone(hass, monkeypatch)
    flow = TccConfigFlow()
    flow.hass = hass
    flow._key = "test-key"
    flow._path = "/config/profiles.yaml"
    flow._service = "mobile_app_phone"
    result = await flow.async_step_test_ok()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "notify"
    assert result["errors"] == {"base": "send_failed"}


async def test_arrival_review_without_a_selected_record_is_refused(hass):
    from custom_components.train_commute_companion.config_flow import TccOptionsFlow

    flow = TccOptionsFlow()
    flow.hass = hass
    result = await flow.async_step_arrival_review({"confirmed": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "arrival_unavailable"


async def test_recovery_offer_without_reviewed_source_is_refused(hass):
    from custom_components.train_commute_companion.config_flow import TccOptionsFlow

    flow = TccOptionsFlow()
    flow.hass = hass
    result = await flow.async_step_recovery_offer({"confirm_location": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "recovery_unavailable"


async def test_superseded_phone_test_cannot_authorize_setup(hass, monkeypatch):
    import asyncio

    from custom_components.train_commute_companion import config_flow

    fake_phone(hass, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send_tests(hass, service, style):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(config_flow, "send_tests", send_tests)
    flow = TccConfigFlow()
    flow.hass = hass
    flow._key = "key"
    flow._path = "/config/profiles.yaml"
    flow._service = "mobile_app_phone"
    pending = asyncio.create_task(flow._test_menu())
    await entered.wait()
    flow._style = "critical"
    release.set()
    await pending
    result = await flow.async_step_test_ok()
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "send_failed"}


async def test_profile_save_without_a_reviewed_draft_is_refused(hass):
    import pytest

    from custom_components.train_commute_companion.config_flow import TccOptionsFlow
    from custom_components.train_commute_companion.profile_editor import EditorError

    flow = TccOptionsFlow()
    flow.hass = hass
    with pytest.raises(EditorError, match="invalid_profile"):
        await flow._save_profile()


async def test_webhook_test_cannot_overwrite_newer_entry_options(hass, monkeypatch):
    import asyncio

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.train_commute_companion import config_flow
    from custom_components.train_commute_companion.const import DOMAIN
    from train_commute_companion.webhook import WebhookTestResult

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "key"},
        options={"notify_service": "mobile_app_phone", "profiles_path": "/config/profiles.yaml"},
    )
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "webhooks"}
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def test_urls(*args, **kwargs):
        entered.set()
        await release.wait()
        return [WebhookTestResult("hooks.example.invalid", 200, 200)]

    monkeypatch.setattr(config_flow, "send_webhook_tests", test_urls)
    task = asyncio.create_task(
        hass.config_entries.options.async_configure(
            flow["flow_id"],
            {"webhook_urls": "https://hooks.example.invalid/events", "webhook_secret": "s" * 40},
        )
    )
    await entered.wait()
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "profiles_path": "/config/newer.yaml"}
    )
    release.set()
    result = await task
    assert result["type"] is FlowResultType.FORM
    assert entry.options["profiles_path"] == "/config/newer.yaml"


async def _stale_settings_submission(hass, monkeypatch, stage):
    import asyncio
    from unittest.mock import AsyncMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.train_commute_companion import config_flow
    from custom_components.train_commute_companion.const import DOMAIN

    fake_phone(hass, monkeypatch)
    fake_phone(hass, monkeypatch, name="mobile_app_new_phone")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_key": "key"},
        options={
            "profiles_path": "/config/original.yaml",
            "notify_service": "mobile_app_phone",
            "loud_style": "time_sensitive",
        },
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def check_path(hass, path):
        if stage == "check_path":
            entered.set()
            await release.wait()

    async def send_tests(*args):
        if stage == "send_tests":
            entered.set()
            await release.wait()
        return True

    monkeypatch.setattr(config_flow, "check_path", check_path)
    monkeypatch.setattr(config_flow, "send_tests", send_tests)
    task = asyncio.create_task(
        hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "profiles_path": "/config/candidate-a.yaml",
                "notify_service": "mobile_app_phone",
                "loud_style": "critical",
            },
        )
    )
    if stage != "confirmation":
        await entered.wait()
    else:
        result = await task
        assert result["type"] is FlowResultType.MENU
    newer = {
        **entry.options,
        "profiles_path": "/config/newer-b.yaml",
        "notify_service": "mobile_app_new_phone",
        "loud_style": "time_sensitive",
    }
    hass.config_entries.async_update_entry(entry, options=newer)
    release.set()
    if stage != "confirmation":
        result = await task
    if result["type"] is FlowResultType.MENU:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "test_ok"}
        )
    assert result["type"] is FlowResultType.FORM
    assert dict(entry.options) == newer


async def test_settings_path_validation_cannot_overwrite_newer_options(hass, monkeypatch):
    await _stale_settings_submission(hass, monkeypatch, "check_path")


async def test_settings_phone_test_cannot_overwrite_newer_options(hass, monkeypatch):
    await _stale_settings_submission(hass, monkeypatch, "send_tests")


async def test_settings_confirmation_cannot_overwrite_newer_options(hass, monkeypatch):
    await _stale_settings_submission(hass, monkeypatch, "confirmation")

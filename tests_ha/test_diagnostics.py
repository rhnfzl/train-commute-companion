import json
from dataclasses import replace

import pytest
from ha_rig import PROFILE, setup_tcc
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.train_commute_companion.const import DOMAIN
from custom_components.train_commute_companion.diagnostics import async_get_config_entry_diagnostics


@pytest.mark.usefixtures("tcc_freezer")
async def test_download_reports_health_without_credentials_or_travel_details(
    hass, hass_client, hass_admin_user
):
    h = await setup_tcc(hass, PROFILE)
    hass.config_entries.async_update_entry(
        h.entry,
        options={
            **h.entry.options,
            "webhook_urls": ["https://receiver.example/private-path"],
            "webhook_secret": "private-signing-secret",
            "webhook_token": "private-bearer-token",
        },
    )
    hass.config_entries.async_update_entry(h.entry, data={**h.entry.data, "api_key": "private-key"})
    h.entry.runtime_data.coordinator.async_set_updated_data(
        replace(h.entry.runtime_data.coordinator.data, refusal="private-error-with-location")
    )
    token = hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_admin_user, client_id="test-client")
    )

    async def client():
        return await hass_client(token)

    data = await get_diagnostics_for_config_entry(hass, client, h.entry)
    assert data["runtime"]["started"] is True
    assert data["runtime"]["profile_count"] == 2
    assert data["runtime"]["profile_refused"] is True
    assert data["settings"]["webhooks_configured"] is True
    text = json.dumps(data)
    for private in (
        "private-key",
        "private-signing-secret",
        "private-bearer-token",
        "private-path",
        "private-error-with-location",
        "office_a",
        "AAA",
        "RTD",
        "profiles.yaml",
        "mobile_app_phone",
    ):
        assert private not in text


async def test_diagnostics_when_entry_has_never_started(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"api_key": "private-key"})
    entry.add_to_hass(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["runtime"] == {"available": False}
    assert "private-key" not in json.dumps(data)


@pytest.mark.usefixtures("tcc_freezer")
async def test_diagnostics_marks_stopped_host_unavailable(hass):
    h = await setup_tcc(hass, PROFILE)
    await h.host.stop()
    data = await async_get_config_entry_diagnostics(hass, h.entry)
    assert data["runtime"]["available"] is False

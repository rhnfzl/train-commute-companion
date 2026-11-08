from unittest.mock import patch

import pytest
from ha_rig import PROFILE, setup_tcc
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.data_entry_flow import FlowResultType

from custom_components.train_commute_companion.const import DOMAIN
from train_commute_companion.providers.base import AuthError, ProviderError

CHECK_KEY = "custom_components.train_commute_companion.config_flow.check_key"


@pytest.mark.usefixtures("tcc_freezer")
async def test_saving_current_key_does_not_restart_monitoring(hass):
    h = await setup_tcc(hass, PROFILE)
    original = h.entry.runtime_data
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": h.entry.entry_id}
    )
    with patch(CHECK_KEY, return_value=frozenset({"AAA", "UT", "RTD"})):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_key": h.entry.data["api_key"]}
        )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert h.entry.runtime_data is original


@pytest.mark.usefixtures("tcc_freezer")
async def test_rotate_key_before_rejection_preserves_options_and_monitoring(hass):
    h = await setup_tcc(hass, PROFILE)
    old_runtime = h.entry.runtime_data
    options = dict(h.entry.options)
    pushes = len(h.pushes)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": h.entry.entry_id}
    )
    assert result["step_id"] == "reconfigure"
    with patch(CHECK_KEY, return_value=frozenset({"AAA", "UT", "RTD"})):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_key": "replacement-key"}
        )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert h.entry.data["api_key"] == "replacement-key"
    assert h.entry.data["started"] is True
    assert dict(h.entry.options) == options
    assert h.entry.runtime_data is not old_runtime
    assert h.entry.runtime_data.host.alive
    assert len(h.pushes) == pushes


@pytest.mark.parametrize(
    "failure,error", [(AuthError, "invalid_auth"), (ProviderError, "cannot_connect")]
)
@pytest.mark.usefixtures("tcc_freezer")
async def test_bad_rotation_leaves_working_key_and_runtime_unchanged(hass, failure, error):
    h = await setup_tcc(hass, PROFILE)
    data, runtime = dict(h.entry.data), h.entry.runtime_data
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": h.entry.entry_id}
    )
    with patch(CHECK_KEY, side_effect=failure):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_key": "bad-key"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert dict(h.entry.data) == data
    assert h.entry.runtime_data is runtime

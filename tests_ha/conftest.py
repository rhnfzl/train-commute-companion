from collections.abc import AsyncIterator

import freezegun
import ha_rig
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

# Bind `custom_components` to this repo first: the phcc test config folder ships its own regular
# `custom_components` package that would otherwise shadow ours and hide the integration.
import custom_components.train_commute_companion  # noqa: F401
from custom_components.train_commute_companion.const import DOMAIN


@pytest.fixture
def hass_config_dir(tmp_path) -> str:
    """Profile and state files must not be shared by test instances or pytest processes."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def tcc_freezer(hass: HomeAssistant) -> AsyncIterator[FrozenDateTimeFactory]:
    """Opt-in (never autouse). Plain phcc `freezer` also freezes the monotonic time asyncio timers
    read, so the runtime loop's sleeps and timeouts never fire; real_asyncio keeps them real. The
    core is ignored: patched `date` names break its serde type checks, and it reads no wall clock.
    Unloads every entry of the test at the end, so no runtime thread lingers."""
    ha_rig.PATCH = pytest.MonkeyPatch()
    try:
        with freezegun.freeze_time(
            real_asyncio=True, ignore=["train_commute_companion"]
        ) as freezer:
            ha_rig.FREEZER = freezer
            yield freezer
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_RETRY):
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        ha_rig.PATCH.undo()
        ha_rig.FREEZER = ha_rig.PATCH = None

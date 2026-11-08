from pathlib import Path

import pytest
from ha_rig import PROFILE, profiles_path, setup_tcc, state_path

pytestmark = pytest.mark.usefixtures("tcc_freezer")


@pytest.mark.parametrize("name", ["isolated_a", "isolated_b"])
async def test_profile_and_state_files_belong_to_each_test_instance(hass, tmp_path, name):
    assert Path(hass.config.config_dir).resolve() == tmp_path.resolve()
    assert not profiles_path(hass).exists()
    h = await setup_tcc(hass, PROFILE.replace("office_a", name))
    assert profiles_path(hass).is_relative_to(tmp_path)
    assert state_path(hass, h.entry).is_relative_to(tmp_path)
    assert state_path(hass, h.entry).exists()
    assert name in {d.profile_name for d in h.entry.runtime_data.coordinator.data.directions}

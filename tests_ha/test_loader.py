import tomllib
from pathlib import Path

from homeassistant.loader import async_get_integration

from custom_components.train_commute_companion.const import DOMAIN


async def test_loader_finds_integration(hass):
    integration = await async_get_integration(hass, DOMAIN)
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert str(integration.version) == project["version"]  # the manifest follows the core
    assert integration.requirements == [f"train-commute-companion=={project['version']}"]
    assert integration.single_config_entry

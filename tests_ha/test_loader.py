from homeassistant.loader import async_get_integration

from custom_components.train_commute_companion.const import DOMAIN


async def test_loader_finds_integration(hass):
    integration = await async_get_integration(hass, DOMAIN)
    assert str(integration.version) == "0.1.0"
    assert integration.single_config_entry

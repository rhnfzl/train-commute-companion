"""Config flow. HA imports this platform to set up any entry; Task 7 adds the steps."""

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class TccConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

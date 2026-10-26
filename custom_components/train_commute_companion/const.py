"""Constants of the Home Assistant shell."""

from homeassistant.const import Platform

DOMAIN = "train_commute_companion"
ATTRIBUTION = "Data: NS"
EVENT_ALERT = "train_commute_companion_alert"
CONF_PROFILES_PATH = "profiles_path"
CONF_NOTIFY = "notify_service"
CONF_STARTED = "started"
REFRESH_S = 15
NOTIFY_TIMEOUT_S = 20
# grows as each platform module lands, so setup never forwards a platform that does not exist
PLATFORMS: list[Platform] = []

"""Constants of the Home Assistant shell."""

from homeassistant.const import Platform

DOMAIN = "train_commute_companion"
ATTRIBUTION = "Data: NS"
EVENT_ALERT = "train_commute_companion_alert"
CONF_PROFILES_PATH = "profiles_path"
CONF_NOTIFY = "notify_service"
CONF_STARTED = "started"
CONF_LOUD_STYLE = "loud_style"
REFRESH_S = 15
NOTIFY_TIMEOUT_S = 20
# HA's 10 s local-push confirm window, then its 10 s remote fallback, plus margin
LOCAL_PUSH_SETTLE_S = 22
NOTIFY_BACKOFF_S = 60
LOUD_STYLES = ("time_sensitive", "critical")
DEFAULT_LOUD_STYLE = "time_sensitive"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

"""Constants of the Home Assistant shell."""

from typing import Literal, get_args

from homeassistant.const import Platform

DOMAIN = "train_commute_companion"
EVENT_ALERT = "train_commute_companion_alert"
CONF_PROFILES_PATH = "profiles_path"
CONF_NOTIFY = "notify_service"
CONF_STARTED = "started"
CONF_LOUD_STYLE = "loud_style"
CONF_NOTIFICATIONS_ENABLED_ENTITY = "notifications_enabled_entity"
CONF_NOTIFICATIONS_QUIET_ENTITY = "notifications_quiet_entity"
CONF_WEBHOOK_URLS = "webhook_urls"
CONF_WEBHOOK_SECRET = "webhook_secret"
CONF_WEBHOOK_TOKEN = "webhook_token"
REFRESH_S = 15
NOTIFY_TIMEOUT_S = 20
# HA's 10 s local-push confirm window, then its 10 s remote fallback, plus margin
LOCAL_PUSH_SETTLE_S = 22
NOTIFY_BACKOFF_S = 60
LoudStyle = Literal["time_sensitive", "critical"]
LOUD_STYLES: tuple[LoudStyle, ...] = get_args(LoudStyle)
DEFAULT_LOUD_STYLE: LoudStyle = "time_sensitive"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

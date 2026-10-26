"""Config flow: the entry exists only once the user confirmed both test pushes (rule 16)."""

import asyncio
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from train_commute_companion.model import Severity
from train_commute_companion.providers.base import AuthError, ProviderError
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.runtime import SystemClock

from .const import (
    CONF_LOUD_STYLE,
    CONF_NOTIFY,
    CONF_PROFILES_PATH,
    DEFAULT_LOUD_STYLE,
    DOMAIN,
    LOUD_STYLES,
)
from .notifier import async_push, notify_payload
from .validate import check_path, validate_file

MOBILE_PREFIX = "mobile_app_"
HOST_TIMEOUT_S = 10
TEST_LOUD = "Train Commute Companion test: this is how a loud alert arrives."
TEST_SILENT = "Train Commute Companion test: this is how a silent update arrives."
TITLE = "Train Commute Companion"


async def check_key(hass: HomeAssistant, api_key: str) -> frozenset[str] | str:
    """The NS station list, or the error key for the form."""
    provider = NsProvider(api_key, get_async_client(hass), SystemClock().now)
    try:
        return await provider.stations()
    except AuthError:
        return "invalid_auth"
    except ProviderError:
        return "cannot_connect"


def notify_services(hass: HomeAssistant) -> list[str]:
    names = hass.services.async_services_for_domain("notify")
    return sorted(n for n in names if n.startswith(MOBILE_PREFIX))


async def send_tests(hass: HomeAssistant, service: str, loud_style: str) -> bool:
    """Both test pushes, in the style the user picked; one failing is enough to refuse."""
    loud = await async_push(hass, service, notify_payload(Severity.LOUD, TEST_LOUD, loud_style))
    silent = await async_push(hass, service, notify_payload(Severity.SILENT, TEST_SILENT))
    return loud and silent


def notify_schema(hass: HomeAssistant, service: str | None, style: str) -> vol.Schema:
    services = notify_services(hass)
    pick = {"default": service} if service in services else {}
    return vol.Schema(
        {
            vol.Required(CONF_NOTIFY, **pick): SelectSelector(
                SelectSelectorConfig(options=services, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_LOUD_STYLE, default=style): SelectSelector(
                SelectSelectorConfig(
                    options=list(LOUD_STYLES),
                    mode=SelectSelectorMode.LIST,
                    translation_key=CONF_LOUD_STYLE,
                )
            ),
        }
    )


class _TestPushMixin:
    """The notify step and its `test` menu, shared by the setup flow and the options flow."""

    hass: HomeAssistant
    _service: str = ""
    _style: str = DEFAULT_LOUD_STYLE
    # each flow defines _failed(): the form to show again when a test push fails
    _back: str  # the step the menu's "pick another target" returns to

    async def _test_menu(self) -> ConfigFlowResult:
        if not await send_tests(self.hass, self._service, self._style):
            return self._failed()
        return await self.async_step_test()

    async def async_step_test(self, user_input: None = None) -> ConfigFlowResult:
        # HA wants a step method behind every step id a result names, a menu's included
        return self.async_show_menu(
            step_id="test",
            menu_options=["test_ok", "test_resend", self._back],
            description_placeholders={"service": self._service},
        )

    async def async_step_test_resend(self, user_input: None = None) -> ConfigFlowResult:
        return await self._test_menu()


class TccConfigFlow(_TestPushMixin, ConfigFlow, domain=DOMAIN):  # type: ignore[misc]
    VERSION = 1
    _back = "notify"

    def __init__(self) -> None:
        self._key = ""
        self._path = ""

    def _notify_form(self, error: bool = False) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="notify",
            data_schema=notify_schema(self.hass, self._service or None, self._style),
            errors={"base": "send_failed"} if error else {},
            description_placeholders={"service": self._service},
        )

    def _failed(self) -> ConfigFlowResult:
        return self._notify_form(error=True)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> "TccOptionsFlow":
        return TccOptionsFlow()

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        default = self.hass.config.path(DOMAIN, "profiles.yaml")
        path = default
        if user_input is not None:
            path = user_input[CONF_PROFILES_PATH]
            key = user_input[CONF_API_KEY]
            if isinstance(got := await check_key(self.hass, key), str):
                errors["base"] = got
            if (bad := await check_path(self.hass, path)) is not None:
                errors[CONF_PROFILES_PATH] = bad
            if not errors:
                self._key, self._path = key, path
                return await self.async_step_notify()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_PROFILES_PATH, default=path): str,
                }
            ),
            errors=errors,
            description_placeholders={"path": path},
        )

    async def async_step_notify(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not notify_services(self.hass):
            return self.async_abort(reason="no_mobile_app")
        if user_input is None:
            return self._notify_form()
        self._service, self._style = user_input[CONF_NOTIFY], user_input[CONF_LOUD_STYLE]
        return await self._test_menu()

    async def async_step_test_ok(self, user_input: None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            title=TITLE,
            data={CONF_API_KEY: self._key},
            options={
                CONF_PROFILES_PATH: self._path,
                CONF_NOTIFY: self._service,
                CONF_LOUD_STYLE: self._style,
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY]
            if isinstance(got := await check_key(self.hass, key), str):
                errors["base"] = got
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={CONF_API_KEY: key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )


class TccOptionsFlow(_TestPushMixin, OptionsFlowWithReload):  # type: ignore[misc]
    _back = "settings"

    def __init__(self) -> None:
        self._path = ""

    async def async_step_init(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options
        self._path = opts.get(CONF_PROFILES_PATH, "")
        self._service = opts[CONF_NOTIFY]
        self._style = opts.get(CONF_LOUD_STYLE, DEFAULT_LOUD_STYLE)
        return self.async_show_menu(step_id="init", menu_options=["validate", "settings"])

    async def _known_stations(self) -> tuple[frozenset[str] | None, str | None]:
        """The station list and, when it could not be had, the report line saying why."""
        entry = self.config_entry
        if hasattr(entry, "runtime_data"):
            try:
                async with asyncio.timeout(HOST_TIMEOUT_S):
                    return await entry.runtime_data.host.run(lambda rt: rt.known_stations), None
            except (RuntimeError, TimeoutError):
                pass  # the runtime is not answering: ask NS directly
        got = await check_key(self.hass, entry.data[CONF_API_KEY])
        if isinstance(got, str):
            return None, "NS key rejected" if got == "invalid_auth" else "NS not reachable"
        return got, None

    async def async_step_validate(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_abort(reason="validated")  # nothing saved, so no reload
        known, why = await self._known_stations()
        lines = await validate_file(self.hass, self.config_entry.options[CONF_PROFILES_PATH], known)
        if why:
            lines.insert(0, why)
        return self.async_show_form(
            step_id="validate",
            data_schema=vol.Schema({}),
            description_placeholders={"report": "\n".join(lines) or "No problems found."},
        )

    def _settings_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        if not notify_services(self.hass):
            return self.async_abort(reason="no_mobile_app")
        schema = vol.Schema({vol.Required(CONF_PROFILES_PATH, default=self._path): str}).extend(
            notify_schema(self.hass, self._service, self._style).schema
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={"path": self._path, "service": self._service},
        )

    def _failed(self) -> ConfigFlowResult:
        return self._settings_form({"base": "send_failed"})

    async def async_step_settings(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self._settings_form({})
        opts = self.config_entry.options
        self._path = user_input[CONF_PROFILES_PATH]
        self._service, self._style = user_input[CONF_NOTIFY], user_input[CONF_LOUD_STYLE]
        if (bad := await check_path(self.hass, self._path)) is not None:
            return self._settings_form({CONF_PROFILES_PATH: bad})
        if (self._service, self._style) != (
            opts[CONF_NOTIFY],
            opts.get(CONF_LOUD_STYLE, DEFAULT_LOUD_STYLE),
        ):
            return await self._test_menu()
        return await self.async_step_test_ok()

    async def async_step_test_ok(self, user_input: None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            data={
                CONF_PROFILES_PATH: self._path,
                CONF_NOTIFY: self._service,
                CONF_LOUD_STYLE: self._style,
            }
        )

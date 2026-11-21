"""Config flow: the entry exists only once the user confirmed both test pushes (rule 16)."""

import asyncio
import os
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from train_commute_companion.model import NotificationChannel, Severity, Stations
from train_commute_companion.providers.base import AuthError, ProviderAnswer, ProviderError, Query
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.runtime import Runtime
from train_commute_companion.state import PollFailure
from train_commute_companion.webhook import check_secret, check_token, parse_urls
from train_commute_companion.webhook_send import send_webhook_tests

from . import host as host_mod
from . import notifier as notifier_mod
from .arrival_flow import ArrivalFlow
from .const import (
    CONF_KNMI_API_KEY,
    CONF_LOUD_STYLE,
    CONF_NOTIFICATIONS_ENABLED_ENTITY,
    CONF_NOTIFICATIONS_QUIET_ENTITY,
    CONF_NOTIFY,
    CONF_PROFILES_PATH,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_TOKEN,
    CONF_WEBHOOK_URLS,
    DEFAULT_LOUD_STYLE,
    DOMAIN,
    LOUD_STYLES,
    LoudStyle,
)
from .flow_state import FlowProof, ProofPhase
from .notifier import async_push, notify_payload
from .profile_editor import EditorError
from .profile_flow import ProfileEditorFlow
from .profile_subentries import PROFILE_SUBENTRY, ProfileSubentryFlow
from .recovery_flow import RecoveryFlow
from .validate import check_path, file_problem, validate_file

MOBILE_PREFIX = "mobile_app_"
HOST_TIMEOUT_S = 10
TEST_LOUD = "Train Commute Companion test: this is how a loud alert arrives."
TEST_SILENT = "Train Commute Companion test: this is how a silent update arrives."
TITLE = "Train Commute Companion"
WEBHOOKS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_WEBHOOK_URLS): TextSelector(TextSelectorConfig(multiline=True)),
        vol.Optional(CONF_WEBHOOK_SECRET): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_WEBHOOK_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


@dataclass(frozen=True, slots=True)
class _SettingsCandidate:
    base_options: dict[str, Any]
    path: str
    service: str
    style: LoudStyle
    target: str | None
    controls: dict[str, str]


def _fails(check: Callable[[str], None], value: str) -> bool:
    try:
        check(value)
    except ValueError:
        return True
    return False


async def check_key(hass: HomeAssistant, api_key: str) -> frozenset[str]:
    """The NS station list; AuthError or ProviderError when it cannot be had."""
    return await NsProvider(api_key, get_async_client(hass), host_mod.make_clock().now).stations()


async def key_error(hass: HomeAssistant, api_key: str) -> str | None:
    """The form's error key for an API key NS does not take, else None."""
    _, error = await _checked_stations(hass, api_key)
    return error


async def _checked_stations(
    hass: HomeAssistant, api_key: str
) -> tuple[frozenset[str] | None, str | None]:
    try:
        return await check_key(hass, api_key), None
    except AuthError:
        return None, "invalid_auth"
    except ProviderError:
        return None, "cannot_connect"


def notify_services(hass: HomeAssistant) -> list[str]:
    names = hass.services.async_services_for_domain("notify")
    return sorted(
        n for n in names if n.startswith(MOBILE_PREFIX) and notifier_mod.mobile_target(hass, n)
    )


async def send_tests(hass: HomeAssistant, service: str, loud_style: LoudStyle) -> bool:
    """Both test pushes, in the style the user picked; one failing is enough to refuse."""
    loud = await async_push(hass, service, notify_payload(Severity.LOUD, TEST_LOUD, loud_style))
    silent = await async_push(hass, service, notify_payload(Severity.SILENT, TEST_SILENT))
    return loud and silent


def picked_phone(hass: HomeAssistant, service: str | None) -> str | None:
    services = notify_services(hass)
    if service not in services:  # one phone is the only sensible pick
        service = services[0] if len(services) == 1 else None
    return service


def notify_schema(hass: HomeAssistant, service: str | None, style: LoudStyle) -> vol.Schema:
    services = notify_services(hass)
    service = picked_phone(hass, service)
    pick = {"default": service} if service else {}
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
    _style: LoudStyle = DEFAULT_LOUD_STYLE
    _failed: Callable[[], ConfigFlowResult]  # the form to show again when a test push fails
    _back: str  # the step the menu's "pick another target" returns to
    async_show_menu: Callable[..., ConfigFlowResult]  # from the flow class it is mixed into
    _push_proof: FlowProof | None = None

    def _proof(self) -> FlowProof:
        if self._push_proof is None:
            self._push_proof = FlowProof()
        return self._push_proof

    def _notify_identity(self) -> tuple[str, LoudStyle, str | None]:
        return self._service, self._style, notifier_mod.mobile_target(self.hass, self._service)

    async def _test_menu(self) -> ConfigFlowResult:
        identity = self._notify_identity()
        proof = self._proof()
        if proof.phase in (ProofPhase.COMMITTING, ProofPhase.COMPLETE):
            return self._failed()
        version = proof.begin(identity)
        if (
            not await send_tests(self.hass, self._service, self._style)
            or identity != self._notify_identity()
            or not proof.accept(version, identity)
        ):
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


class TccConfigFlow(ProfileEditorFlow[ConfigFlowResult], _TestPushMixin, ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _back = "notify"

    @classmethod
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {PROFILE_SUBENTRY: ProfileSubentryFlow}

    def __init__(self) -> None:
        self._key = ""
        self._path = ""
        self._key_proof = FlowProof()

    async def _replacement_key_error(self, key: str) -> str | None:
        proof = self._key_proof
        if proof.phase in (ProofPhase.COMMITTING, ProofPhase.COMPLETE):
            return "cannot_connect"
        version = proof.begin(key)
        error = await key_error(self.hass, key)
        if error is not None:
            return error
        if not proof.accept(version, key) or not proof.claim(key):
            return "cannot_connect"
        proof.finish()
        return None

    def _notify_form(self, error: bool = False) -> ConfigFlowResult:
        phone = picked_phone(self.hass, self._service or None)
        return self.async_show_form(
            step_id="notify",
            data_schema=notify_schema(self.hass, phone, self._style),
            errors={"base": "send_failed"} if error else {},
            description_placeholders={
                "service": self._service,
                "phone": f"notify.{phone}" if phone else "the phone you pick",
            },
        )

    def _failed(self) -> ConfigFlowResult:
        return self._notify_form(error=True)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "TccOptionsFlow":
        return TccOptionsFlow()

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY]
            stations, bad_key = await _checked_stations(self.hass, key)
            if bad_key is not None:
                errors["base"] = bad_key
            else:
                self._key = key
                path = self.hass.config.path(DOMAIN, "profiles.yaml")
                # a file already there (a reinstall) is used, never written over
                if user_input.get("existing_file", False) or await self.hass.async_add_executor_job(
                    os.path.exists, path
                ):
                    return await self.async_step_profiles_file()
                self._path = path
                if user_input.get("skip_profile", False):
                    return await self.async_step_notify()
                return await self._begin_editor(
                    key,
                    path,
                    create=True,
                    catalogue=stations if isinstance(stations, Stations) else None,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional("existing_file", default=False): BooleanSelector(),
                    vol.Optional("skip_profile", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_profiles_file(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        path = self.hass.config.path(DOMAIN, "profiles.yaml")
        report = ""
        if user_input is not None:
            path = user_input[CONF_PROFILES_PATH]
        if user_input is not None and (bad := await check_path(self.hass, path)) is not None:
            errors[CONF_PROFILES_PATH] = bad
        elif report := await self.hass.async_add_executor_job(file_problem, path):
            errors[CONF_PROFILES_PATH] = "file_broken"  # shown until the file is fixed
        elif user_input is not None:
            self._path = path
            return await self.async_step_notify()
        return self.async_show_form(
            step_id="profiles_file",
            data_schema=vol.Schema({vol.Required(CONF_PROFILES_PATH, default=path): str}),
            errors=errors,
            description_placeholders={"path": path, "report": report},
        )

    async def async_step_profile_error(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        # a file appeared where guided setup would create one: offer it, never write over it
        if self._editor_create and await self.hass.async_add_executor_job(
            os.path.exists, self._editor_path
        ):
            self._document = self._pending_profile = None
            return await self.async_step_profiles_file()
        return await super().async_step_profile_error(user_input)

    async def async_step_notify(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._proof().phase in (ProofPhase.COMMITTING, ProofPhase.COMPLETE):
            return self._failed()
        if not notify_services(self.hass):
            return self.async_abort(reason="no_mobile_app")
        if user_input is None:
            return self._notify_form()
        self._service, self._style = user_input[CONF_NOTIFY], user_input[CONF_LOUD_STYLE]
        return await self._test_menu()

    async def async_step_test_ok(self, user_input: None = None) -> ConfigFlowResult:
        proof = self._proof()
        if not proof.claim(self._notify_identity()):
            return self._failed()
        guided = self._pending_profile is not None
        if guided:
            try:
                await self._save_profile()
            except EditorError as err:
                proof.retry()
                return self._editor_error(err.code, err.report)
            self._pending_profile = None
        proof.finish()
        return self.async_create_entry(
            title=TITLE,
            description="guided" if guided else None,
            data={CONF_API_KEY: self._key},
            options={
                CONF_PROFILES_PATH: self._path,
                CONF_NOTIFY: self._service,
                CONF_LOUD_STYLE: self._style,
            },
        )

    async def _profile_ready(self) -> ConfigFlowResult:
        return await self.async_step_notify()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY]
            if (bad_key := await self._replacement_key_error(key)) is not None:
                errors["base"] = bad_key
            else:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data_updates={CONF_API_KEY: key},
                    reload_even_if_entry_is_unchanged=False,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reauth_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY]
            if (bad_key := await self._replacement_key_error(key)) is not None:
                errors["base"] = bad_key
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


class TccOptionsFlow(
    ArrivalFlow,
    RecoveryFlow,
    ProfileEditorFlow[ConfigFlowResult],
    _TestPushMixin,
    OptionsFlowWithReload,
):
    _back = "settings"

    async def _preview_trips(self, query: Query) -> ProviderAnswer | PollFailure | None:
        entry = self.config_entry
        if hasattr(entry, "runtime_data"):
            try:
                return await entry.runtime_data.host.run(lambda rt: rt.preview_trip(query))
            except host_mod.RuntimeNotRunning:
                pass
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
        return None

    def _profile_channels(self) -> frozenset[NotificationChannel]:
        return (
            frozenset({"primary", "webhook"})
            if self.config_entry.options.get(CONF_WEBHOOK_URLS)
            else frozenset({"primary"})
        )

    def __init__(self) -> None:
        self._path = ""
        self._webhook_proof = FlowProof()
        self._settings_check = FlowProof()
        self._settings_candidate: _SettingsCandidate | None = None

    def _settings_current(self, candidate: _SettingsCandidate | None) -> bool:
        return (
            candidate is not None
            and self._settings_candidate is candidate
            and dict(self.config_entry.options) == candidate.base_options
            and self._path == candidate.path
            and self._notify_identity() == (candidate.service, candidate.style, candidate.target)
            and candidate.target is not None
        )

    def _stale_settings(self) -> ConfigFlowResult:
        return self._settings_form({"base": "settings_changed"})

    async def _test_menu(self) -> ConfigFlowResult:
        candidate = self._settings_candidate
        if not self._settings_current(candidate):
            return self._stale_settings()
        result = await super()._test_menu()
        return result if self._settings_current(candidate) else self._stale_settings()

    async def async_step_init(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options
        self._path = opts.get(CONF_PROFILES_PATH, "")
        self._service = opts[CONF_NOTIFY]
        self._style = opts.get(CONF_LOUD_STYLE, DEFAULT_LOUD_STYLE)
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "profiles",
                "validate",
                "settings",
                "webhooks",
                "weather",
                "recovery",
                "arrival",
            ],
        )

    async def async_step_weather(
        self,
        user_input: Mapping[str, Any] | None = None,
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_KNMI_API_KEY: user_input.get(CONF_KNMI_API_KEY, "").strip(),
                }
            )
        return self.async_show_form(
            step_id="weather",
            description_placeholders={"knmi_portal": "https://developer.dataplatform.knmi.nl/"},
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_KNMI_API_KEY,
                        default=self.config_entry.options.get(CONF_KNMI_API_KEY, ""),
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
        )

    async def async_step_profiles(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._document is None:
            default_path = self.hass.config.path(DOMAIN, "profiles.yaml")
            create = self._path == default_path and not await self.hass.async_add_executor_job(
                os.path.lexists, self._path
            )
            entry = self.config_entry
            catalogue = None
            if hasattr(entry, "runtime_data"):

                async def profile_stations(rt: Runtime) -> frozenset[str] | None:
                    return rt.known_stations or await rt.stations()

                try:
                    async with asyncio.timeout(HOST_TIMEOUT_S):
                        known = await entry.runtime_data.host.run(profile_stations)
                except (host_mod.RuntimeNotRunning, TimeoutError):
                    return self._editor_error("cannot_connect")
                if not known:
                    return self._editor_error("cannot_connect")
                catalogue = (
                    known if isinstance(known, Stations) else Stations(dict.fromkeys(known, ()))
                )
            return await self._begin_editor(
                entry.data[CONF_API_KEY], self._path, create=create, catalogue=catalogue
            )
        return await super().async_step_profiles(user_input)

    async def _profile_ready(self) -> ConfigFlowResult:
        try:
            await self._save_profile()
        except EditorError as err:
            return self._editor_error(err.code, err.report)
        return self.async_abort(reason="profile_saved")

    async def _known_stations(self) -> tuple[frozenset[str] | None, str | None]:
        """The station list and, when it could not be had, the report line saying why."""
        entry = self.config_entry
        if hasattr(entry, "runtime_data"):
            try:
                async with asyncio.timeout(HOST_TIMEOUT_S):
                    return await entry.runtime_data.host.run(lambda rt: rt.known_stations), None
            except (host_mod.RuntimeNotRunning, TimeoutError):
                pass  # the runtime is not answering: ask NS directly
        try:
            return await check_key(self.hass, entry.data[CONF_API_KEY]), None
        except AuthError:
            return None, "NS key rejected"
        except ProviderError:
            return None, "NS not reachable"

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

    def _settings_form(self, errors: dict[str, str], report: str = "") -> ConfigFlowResult:
        if not notify_services(self.hass):
            return self.async_abort(reason="no_mobile_app")
        schema = vol.Schema({vol.Required(CONF_PROFILES_PATH, default=self._path): str}).extend(
            notify_schema(self.hass, self._service, self._style).schema
        )
        schema = schema.extend(
            {
                vol.Optional(key): EntitySelector(
                    EntitySelectorConfig(domain=["binary_sensor", "input_boolean", "switch"])
                )
                for key in (CONF_NOTIFICATIONS_ENABLED_ENTITY, CONF_NOTIFICATIONS_QUIET_ENTITY)
            }
        )
        suggested = dict(self.config_entry.options)
        if suggested.get(CONF_NOTIFY) not in notify_services(self.hass):
            suggested.pop(CONF_NOTIFY, None)  # a removed phone: one remaining phone is the default
        schema = self.add_suggested_values_to_schema(schema, suggested)
        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "path": self._path,
                "service": self._service,
                "report": report,
            },
        )

    def _failed(self) -> ConfigFlowResult:
        return self._settings_form({"base": "send_failed"})

    async def async_step_settings(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._proof().phase in (ProofPhase.COMMITTING, ProofPhase.COMPLETE):
            return self._failed()
        if user_input is None:
            return self._settings_form({})
        candidate = _SettingsCandidate(
            deepcopy(dict(self.config_entry.options)),
            user_input[CONF_PROFILES_PATH],
            user_input[CONF_NOTIFY],
            user_input[CONF_LOUD_STYLE],
            notifier_mod.mobile_target(self.hass, user_input[CONF_NOTIFY]),
            {
                key: user_input[key]
                for key in (CONF_NOTIFICATIONS_ENABLED_ENTITY, CONF_NOTIFICATIONS_QUIET_ENTITY)
                if user_input.get(key)
            },
        )
        self._settings_candidate = candidate
        self._path, self._service, self._style = candidate.path, candidate.service, candidate.style
        version = self._settings_check.begin(candidate)
        bad = await check_path(self.hass, candidate.path)
        if bad == "file_missing" and candidate.path == candidate.base_options.get(
            CONF_PROFILES_PATH
        ):
            bad = None
        # only a new path is checked for problems: a broken file at the saved path must not
        # block a phone change, and the runtime already reports it
        report = (
            ""
            if bad or candidate.path == candidate.base_options.get(CONF_PROFILES_PATH)
            else await self.hass.async_add_executor_job(file_problem, candidate.path)
        )
        if not self._settings_current(candidate):
            return self._stale_settings()
        if bad is not None:
            return self._settings_form({CONF_PROFILES_PATH: bad})
        if report:
            return self._settings_form({CONF_PROFILES_PATH: "file_broken"}, report)
        if not self._settings_check.accept(version, candidate):
            return self._stale_settings()
        opts = candidate.base_options
        if (self._service, self._style) != (
            opts[CONF_NOTIFY],
            opts.get(CONF_LOUD_STYLE, DEFAULT_LOUD_STYLE),
        ):
            return await self._test_menu()
        proof = self._proof()
        identity = self._notify_identity()
        proof.accept(proof.begin(identity), identity)
        return await self.async_step_test_ok()

    def _webhooks_form(
        self,
        values: Mapping[str, Any],
        errors: dict[str, str] | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        # suggested values, not defaults: a default would refill a field the user cleared
        return self.async_show_form(
            step_id="webhooks",
            data_schema=self.add_suggested_values_to_schema(WEBHOOKS_SCHEMA, values),
            errors=errors or {},
            description_placeholders=placeholders,
        )

    async def async_step_webhooks(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options
        options_snapshot = dict(opts)
        saved = (
            opts.get(CONF_WEBHOOK_URLS, []),
            opts.get(CONF_WEBHOOK_SECRET, ""),
            opts.get(CONF_WEBHOOK_TOKEN, ""),
        )
        if user_input is None:
            return self._webhooks_form(
                {
                    CONF_WEBHOOK_URLS: "\n".join(saved[0]),
                    CONF_WEBHOOK_SECRET: saved[1],
                    CONF_WEBHOOK_TOKEN: saved[2],
                }
            )
        text: str = user_input.get(CONF_WEBHOOK_URLS, "")
        secret: str = user_input.get(CONF_WEBHOOK_SECRET, "")
        token: str = user_input.get(CONF_WEBHOOK_TOKEN, "")
        errors: dict[str, str] = {}
        urls: tuple[str, ...] = ()
        try:
            urls = parse_urls(text)
        except ValueError:
            errors[CONF_WEBHOOK_URLS] = "bad_webhook_url"
        if text.strip() and _fails(check_secret, secret):
            errors[CONF_WEBHOOK_SECRET] = "weak_webhook_secret"
        if token and _fails(check_token, token):
            errors[CONF_WEBHOOK_TOKEN] = "weak_webhook_token"
        if errors:
            return self._webhooks_form(user_input, errors)
        proof = self._webhook_proof
        if proof.phase in (ProofPhase.COMMITTING, ProofPhase.COMPLETE):
            return self._webhooks_form(user_input, {"base": "webhook_test_failed"})
        candidate = (urls, secret, token)
        version = proof.begin(candidate)
        if urls and (list(urls), secret, token) != saved:
            # nothing is saved unless every URL accepted both test sends
            # making the client loads the CA bundle, a blocking call on hass.loop
            client = await self.hass.async_add_executor_job(host_mod.make_webhook_client)
            async with client:
                results = await send_webhook_tests(
                    client, urls, secret, host_mod.make_clock().now, token=token
                )
            if failed := [r.summary() for r in results if not r.ok]:
                return self._webhooks_form(
                    user_input, {"base": "webhook_test_failed"}, {"failed": "; ".join(failed)}
                )
        if (
            dict(self.config_entry.options) != options_snapshot
            or not proof.accept(version, candidate)
            or not proof.claim(candidate)
        ):
            return self._webhooks_form(user_input, {"base": "webhook_test_failed"})
        proof.finish()
        return self.async_create_entry(
            data={
                **opts,
                CONF_WEBHOOK_URLS: list(urls),
                CONF_WEBHOOK_SECRET: secret,
                CONF_WEBHOOK_TOKEN: token,
            }
        )

    async def async_step_test_ok(self, user_input: None = None) -> ConfigFlowResult:
        candidate = self._settings_candidate
        if (
            not self._settings_current(candidate)
            or self._settings_check.phase is not ProofPhase.REVIEWED
        ):
            return self._stale_settings()
        assert candidate is not None
        proof = self._proof()
        if not proof.claim(self._notify_identity()):
            return self._failed()
        if not self._settings_check.claim(candidate):
            proof.retry()
            return self._stale_settings()
        proof.finish()
        self._settings_check.finish()
        return self.async_create_entry(
            data={
                **{
                    key: value
                    for key, value in candidate.base_options.items()
                    if key
                    not in (CONF_NOTIFICATIONS_ENABLED_ENTITY, CONF_NOTIFICATIONS_QUIET_ENTITY)
                },
                **candidate.controls,
                CONF_PROFILES_PATH: candidate.path,
                CONF_NOTIFY: candidate.service,
                CONF_LOUD_STYLE: candidate.style,
            }
        )

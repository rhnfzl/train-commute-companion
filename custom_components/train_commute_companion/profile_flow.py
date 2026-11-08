"""Station-name forms backed by the same portable profile file as every other shell."""

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from train_commute_companion.calendar import TZ, WEEKDAYS
from train_commute_companion.model import NotificationChannel
from train_commute_companion.notification_policy import CATEGORIES
from train_commute_companion.providers.base import AuthError, ProviderError
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.transfer import FALLBACK_MIN

from .flow_state import ReviewPhase
from .profile_editor import (
    EditorError,
    ProfileDocument,
    edit_profile,
    read_document,
    save_document,
    validate_text,
)

CATEGORY_LABELS = {
    "journey": "Journey changes",
    "transfers": "Connections",
    "advice": "Departure advice",
    "delays": "Delays",
    "platforms": "Platform changes",
    "crowding": "Crowd forecasts",
    "planned_works": "Planned works",
    "disruptions": "Active disruptions",
    "routine": "Routine and compensation reminders",
}


async def station_catalogue(hass: HomeAssistant, key: str) -> dict[str, str]:
    return await NsProvider(key, get_async_client(hass), lambda: datetime.now(TZ)).station_names()


class ProfileEditorFlow:
    hass: HomeAssistant
    async_show_form: Callable[..., ConfigFlowResult]
    async_show_menu: Callable[..., ConfigFlowResult]
    _editor_key: str = ""
    _editor_path: str = ""
    _editor_create: bool = False
    _document: ProfileDocument | None = None
    _catalogue: dict[str, str] | None = None
    _profile_id: str | None = None
    _pending_profile: str | None = None
    _editor_values: dict[str, Any] | None = None
    _profile_phase = ReviewPhase.SELECTING

    def _profile_channels(self) -> frozenset[NotificationChannel]:
        return frozenset({"primary"})

    def _roots(self) -> tuple[Path, ...]:
        roots: tuple[Path, ...] = (Path(self.hass.config.config_dir),)
        if not self._editor_create:
            roots += tuple(Path(p) for p in self.hass.config.allowlist_external_dirs)
        return roots

    def _editor_error(self, code: str, report: str = "") -> ConfigFlowResult:
        return self.async_show_form(
            step_id="profile_error",
            data_schema=vol.Schema({}),
            errors={"base": code},
            description_placeholders={"report": report, "path": self._editor_path},
        )

    async def _begin_editor(self, key: str, path: str, *, create: bool = False) -> ConfigFlowResult:
        if self._profile_phase in (ReviewPhase.COMMITTING, ReviewPhase.COMPLETE):
            return self._editor_error("file_changed")
        self._editor_key, self._editor_path, self._editor_create = key, path, create
        self._document = None
        self._pending_profile = None
        try:
            self._catalogue = await station_catalogue(self.hass, key)
            self._document = await self.hass.async_add_executor_job(
                lambda: read_document(
                    Path(path),
                    self._roots(),
                    frozenset(self._catalogue or {}),
                    datetime.now(TZ).date(),
                    create=create,
                )
            )
        except AuthError:
            return self._editor_error("invalid_auth")
        except (ProviderError, TimeoutError):
            return self._editor_error("cannot_connect")
        except EditorError as err:
            return self._editor_error(err.code, err.report)
        if create:
            self._profile_id = None
            self._profile_phase = ReviewPhase.EDITING
            return await self.async_step_profile()
        return await self.async_step_profiles()

    async def async_step_profile_error(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._begin_editor(
            self._editor_key, self._editor_path, create=self._editor_create
        )

    async def async_step_profiles(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._profile_phase in (ReviewPhase.COMMITTING, ReviewPhase.COMPLETE)
            or self._document is None
        ):
            return self._editor_error("invalid_profile")
        profiles = self._document.data["profiles"]
        if user_input is not None:
            selected = user_input.get("profile")
            if selected == "new":
                self._profile_id = None
            elif (
                isinstance(selected, str)
                and selected.startswith("edit:")
                and any(p["id"] == selected[5:] for p in profiles)
            ):
                self._profile_id = selected[5:]
            else:
                return self._editor_error("invalid_profile")
            self._profile_phase = ReviewPhase.EDITING
            return await self.async_step_profile()
        options: list[SelectOptionDict] = [{"value": "new", "label": "Add a profile"}]
        options.extend({"value": f"edit:{p['id']}", "label": p["name"]} for p in profiles)
        return self.async_show_form(
            step_id="profiles",
            data_schema=vol.Schema(
                {
                    vol.Required("profile"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    def _profile_defaults(self) -> dict[str, Any]:
        assert self._document is not None
        profile: dict[str, Any] = next(
            (p for p in self._document.data["profiles"] if p["id"] == self._profile_id), {}
        )
        return {
            "name": profile.get("name", "commute"),
            "days": profile.get("days", ["mon", "tue", "wed", "thu", "fri"]),
            "home_station": profile.get("home_station"),
            "work_station": profile.get("work_station"),
            "skip_public_holidays": profile.get("skip_public_holidays", True),
            "require_accessible": profile.get("accessibility", {}).get("require_accessible", False),
            "disabled_categories": profile.get("alerts", {}).get("disabled_categories", []),
            **{
                f"{direction}_{field}": profile.get(direction, {}).get(source, default)
                for direction in ("outbound", "return")
                for field, source, default in (
                    ("time", "first_train", "08:00" if direction == "outbound" else "17:00"),
                    ("transfers", "transfers", []),
                    ("buffer", "buffer_min", 10),
                )
            },
        }

    def _profile_schema(self, defaults: Mapping[str, Any]) -> vol.Schema:
        options: list[SelectOptionDict] = [
            {"value": code, "label": label}
            for code, label in sorted((self._catalogue or {}).items(), key=lambda item: item[1])
        ]
        station = SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
        )
        transfers = SelectSelector(
            SelectSelectorConfig(
                options=options,
                mode=SelectSelectorMode.DROPDOWN,
                multiple=True,
            )
        )
        categories: list[SelectOptionDict] = [
            {"value": category, "label": CATEGORY_LABELS[category]} for category in CATEGORIES
        ]
        fields: dict[Any, Any] = {
            vol.Required("name"): str,
            vol.Required("home_station"): station,
            vol.Required("work_station"): station,
            vol.Required("days"): SelectSelector(
                SelectSelectorConfig(
                    options=list(WEEKDAYS),
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required("skip_public_holidays"): BooleanSelector(),
            vol.Required("require_accessible"): BooleanSelector(),
            vol.Required("disabled_categories"): SelectSelector(
                SelectSelectorConfig(
                    options=categories,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        for direction in ("outbound", "return"):
            fields[vol.Required(f"{direction}_time")] = str
            fields[vol.Required(f"{direction}_transfers")] = transfers
            fields[vol.Required(f"{direction}_buffer")] = NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            )
        # Suggested values also preserve an intentionally cleared input after a validation error.
        return vol.Schema(
            {
                vol.Required(str(key), description={"suggested_value": defaults[str(key)]}): value
                for key, value in fields.items()
                if str(key) in defaults
            }
        )

    async def async_step_profile(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._profile_phase is not ReviewPhase.EDITING
            or self._document is None
            or self._catalogue is None
        ):
            return self._editor_error("invalid_profile")
        values = dict(user_input) if user_input is not None else self._profile_defaults()
        errors: dict[str, str] = {}
        report = ""
        if user_input is not None:
            try:
                self._pending_profile = edit_profile(
                    self._document,
                    self._profile_id,
                    values,
                    frozenset(self._catalogue),
                    datetime.now(TZ).date(),
                )
            except EditorError as err:
                errors["base"], report = err.code, err.report
            else:
                self._editor_values = values
                return await self.async_step_profile_details()
        return self.async_show_form(
            step_id="profile",
            data_schema=self._profile_schema(values),
            errors=errors,
            description_placeholders={"report": report},
        )

    async def async_step_profile_details(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._profile_phase is not ReviewPhase.EDITING
            or self._document is None
            or self._editor_values is None
            or self._catalogue is None
        ):
            return self._editor_error("invalid_profile")
        existing: dict[str, Any] = next(
            (p for p in self._document.data["profiles"] if p["id"] == self._profile_id), {}
        )
        stations = tuple(
            dict.fromkeys(
                self._editor_values["outbound_transfers"] + self._editor_values["return_transfers"]
            )
        )
        values = dict(user_input or {})
        errors: dict[str, str] = {}
        report = ""
        if user_input is not None:
            minimums = {}
            for code in stations:
                minimum = user_input[f"minimum_{code}"]
                minimums[code] = (
                    int(minimum) if isinstance(minimum, float) and minimum.is_integer() else minimum
                )
            details = {**self._editor_values, **user_input, "transfer_min": minimums}
            try:
                self._pending_profile = edit_profile(
                    self._document,
                    self._profile_id,
                    details,
                    frozenset(self._catalogue),
                    datetime.now(TZ).date(),
                    available_channels=self._profile_channels(),
                )
            except EditorError as err:
                errors["base"], report = err.code, err.report
            else:
                if user_input.get("edit_weekday_times", False):
                    self._editor_values = details
                    return await self.async_step_profile_weekdays()
                self._profile_phase = ReviewPhase.REVIEWING
                return await self._profile_ready()
        fields: dict[Any, Any] = {}
        fields[
            vol.Optional("edit_weekday_times", default=values.get("edit_weekday_times", False))
        ] = BooleanSelector()
        fields[
            vol.Optional(
                "compensation_reminder",
                default=values.get(
                    "compensation_reminder",
                    existing.get("alerts", {}).get("compensation_reminder", False),
                ),
            )
        ] = BooleanSelector()
        for code in stations:
            fields[
                vol.Required(
                    f"minimum_{code}",
                    default=values.get(
                        f"minimum_{code}", existing.get("transfer_min", {}).get(code, FALLBACK_MIN)
                    ),
                )
            ] = NumberSelector(NumberSelectorConfig(min=0, step=1, mode=NumberSelectorMode.BOX))
        pause_options: list[SelectOptionDict] = [
            {"value": "keep", "label": "Keep existing holiday ranges"},
            {"value": "add", "label": "Add a holiday range"},
            {"value": "replace", "label": "Replace holiday ranges with this range"},
            {"value": "clear", "label": "Remove all holiday ranges"},
        ]
        fields[vol.Required("pause_action", default=values.get("pause_action", "keep"))] = (
            SelectSelector(
                SelectSelectorConfig(
                    options=pause_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        )
        for key in ("pause_start", "pause_end"):
            suggested = {"suggested_value": values[key]} if key in values else {}
            fields[vol.Optional(key, description=suggested)] = DateSelector()
        configured = self._profile_channels()
        saved_channels = existing.get("alerts", {}).get("channels", {})
        for category in CATEGORIES:
            key = f"channel_{category}"
            default = (
                ",".join(sorted(saved_channels[category]))
                if category in saved_channels
                else "default"
            )
            selected = values.get(key, default)
            choices: list[SelectOptionDict] = [
                {"value": "default", "label": "All configured destinations"},
                {"value": "primary", "label": "Phone"},
            ]
            if "webhook" in configured:
                choices += [
                    {"value": "webhook", "label": "Webhooks"},
                    {"value": "primary,webhook", "label": "Phone and webhooks"},
                ]
            if selected not in {choice["value"] for choice in choices}:
                choices.append(
                    {"value": selected, "label": "Unavailable destinations: " + selected}
                )
            fields[vol.Optional(key, default=selected)] = SelectSelector(
                SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)
            )
        labels = (
            ", ".join(f"minimum_{code}: {self._catalogue[code]}" for code in stations)
            or "No transfers"
        )
        pauses = (
            ", ".join(f"{p['start']} to {p['end']}" for p in existing.get("pauses", [])) or "None"
        )
        return self.async_show_form(
            step_id="profile_details",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"stations": labels, "pauses": pauses, "report": report},
        )

    async def async_step_profile_weekdays(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._profile_phase is not ReviewPhase.EDITING
            or self._document is None
            or self._editor_values is None
            or self._catalogue is None
        ):
            return self._editor_error("invalid_profile")
        existing: dict[str, Any] = next(
            (p for p in self._document.data["profiles"] if p["id"] == self._profile_id), {}
        )
        values = dict(user_input or {})
        errors: dict[str, str] = {}
        report = ""
        if user_input is not None:
            details = dict(self._editor_values)
            for direction in ("outbound", "return"):
                details[f"{direction}_weekday_times"] = {
                    day: user_input[f"{direction}_{day}"].strip()
                    for day in WEEKDAYS
                    if user_input.get(f"{direction}_{day}", "").strip()
                }
            try:
                self._pending_profile = edit_profile(
                    self._document,
                    self._profile_id,
                    details,
                    frozenset(self._catalogue),
                    datetime.now(TZ).date(),
                    available_channels=self._profile_channels(),
                )
            except EditorError as err:
                errors["base"], report = err.code, err.report
            else:
                self._profile_phase = ReviewPhase.REVIEWING
                return await self._profile_ready()
        fields: dict[Any, Any] = {}
        for direction in ("outbound", "return"):
            saved = existing.get(direction, {}).get("first_train_by_weekday", {})
            for day in WEEKDAYS:
                key = f"{direction}_{day}"
                fields[vol.Optional(key, default=values.get(key, saved.get(day, "")))] = str
        return self.async_show_form(
            step_id="profile_weekdays",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "outbound_default": self._editor_values["outbound_time"],
                "return_default": self._editor_values["return_time"],
                "report": report,
            },
        )

    async def _save_profile(self) -> None:
        if (
            self._profile_phase is not ReviewPhase.REVIEWING
            or self._document is None
            or self._pending_profile is None
            or self._catalogue is None
        ):
            raise EditorError("invalid_profile")
        document, text = self._document, self._pending_profile
        known, roots = frozenset(self._catalogue), self._roots()
        self._profile_phase = ReviewPhase.COMMITTING
        try:
            await self.hass.async_add_executor_job(
                validate_text, text, known, datetime.now(TZ).date(), self._profile_channels()
            )
            if self._document is not document or self._pending_profile != text:
                raise EditorError("file_changed")
            await self.hass.async_add_executor_job(save_document, document, text, roots)
        except EditorError:
            self._profile_phase = ReviewPhase.REVIEWING
            raise
        self._profile_phase = ReviewPhase.COMPLETE

    async def _profile_ready(self) -> ConfigFlowResult:
        raise NotImplementedError

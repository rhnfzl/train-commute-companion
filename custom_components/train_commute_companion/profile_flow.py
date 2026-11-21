"""Station-name forms backed by the same portable profile file as every other shell."""

from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from itertools import count
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
from train_commute_companion.model import NotificationChannel, Stations
from train_commute_companion.notification_policy import CATEGORIES
from train_commute_companion.profile import slug
from train_commute_companion.providers.base import AuthError, ProviderError
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.transfer import FALLBACK_MIN

from .flow_state import ReviewPhase
from .profile_editor import (
    OTHER_STATION_DEFAULTS,
    EditorError,
    ProfileDocument,
    coded,
    edit_profile,
    read_document,
    save_document,
    validate_text,
)

MODE_OPTIONS: list[SelectOptionDict] = [
    {"value": "fixed", "label": "Fixed: follow my usual train"},
    {"value": "window", "label": "Window: follow any train between two times"},
]
PICK_OPTIONS: list[SelectOptionDict] = [
    {"value": "earliest", "label": "Earliest train, missed trains make a sound"},
    {"value": "earliest_silent", "label": "Earliest train, missed trains are silent"},
    {"value": "on_leave", "label": "Pick the train when I leave"},
]
DAY_NAMES = dict(
    zip(WEEKDAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))
)
_OPTIONAL_TEXT = ("time", "window_start", "window_end")
_SINGLE_STATIONS = ("home_station", "work_station")

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


async def station_catalogue(hass: HomeAssistant, key: str) -> Stations:
    return await NsProvider(key, get_async_client(hass), lambda: datetime.now(TZ)).stations()


class ProfileEditorFlow:
    hass: HomeAssistant
    async_show_form: Callable[..., ConfigFlowResult]
    async_show_menu: Callable[..., ConfigFlowResult]
    _editor_key: str = ""
    _editor_path: str = ""
    _editor_create: bool = False
    _document: ProfileDocument | None = None
    _catalogue: Stations | None = None
    _profile_id: str | None = None
    _pending_profile: str | None = None
    _editor_values: dict[str, Any] | None = None
    _day_target: tuple[str, str] | None = None
    _profile_phase = ReviewPhase.SELECTING

    def _profile_channels(self) -> frozenset[NotificationChannel]:
        return frozenset({"primary"})

    def _labels(self) -> dict[str, str]:
        known = self._catalogue or Stations({})
        return {code: known.name(code) for code in known}

    def _station_codes(self) -> dict[str, str]:
        """Name -> code for the single station pickers. The picker shows its raw value, so the
        value is the name; a name two stations share gets its code added, again until no other
        label has it, so every station keeps exactly one label."""
        labels = self._labels()
        shared = Counter(labels.values())
        out = {name: code for code, name in labels.items() if shared[name] == 1}
        for code, name in sorted(labels.items()):
            if shared[name] > 1:
                label = f"{name} ({code})"
                while label in out:
                    label += f" ({code})"
                out[label] = code
        return out

    def _station_code(self, text: object) -> str | None:
        """The code for a picked label, else for the one station a typed name or code means."""
        assert self._catalogue is not None
        if not isinstance(text, str):
            return None
        codes = self._station_codes()
        if (code := codes.get(text, codes.get(text.strip()))) is not None:
            return code
        found = self._catalogue.lookup(text.strip())  # case, spaces, aliases; never a guess
        return found[0] if len(found) == 1 else None

    def _existing(self) -> dict[str, Any]:
        """The profile being edited, its stations as codes for the form."""
        assert self._document is not None and self._catalogue is not None
        found = (p for p in self._document.data["profiles"] if p["id"] == self._profile_id)
        return coded(next(found, {}), self._catalogue)

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
            known = self._catalogue = await station_catalogue(self.hass, key)
            if not known:  # NS answered without stations: nothing could be picked or checked
                return self._editor_error("cannot_connect")
            self._document = await self.hass.async_add_executor_job(
                lambda: read_document(
                    Path(path),
                    self._roots(),
                    known,
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
                            translation_key="profile_choice",
                        )
                    ),
                }
            ),
        )

    def _free_name(self) -> str:
        """commute, else commute 2, commute 3: names must stay unique in the file."""
        assert self._document is not None
        taken = {slug(str(p.get("name", ""))) for p in self._document.data["profiles"]}
        names = (f"commute {i}" if i > 1 else "commute" for i in count(1))
        return next(name for name in names if slug(name) not in taken)

    def _day_clash(self, days: object) -> str | None:
        """The name of another profile that already runs on one of `days`, the same
        shared-weekday rule the file check applies, said in the form's own words."""
        assert self._document is not None
        wanted = {d for d in days if isinstance(d, str)} if isinstance(days, list) else set()
        for p in self._document.data["profiles"]:
            theirs = p.get("days", [])
            if (
                p["id"] != self._profile_id
                and isinstance(theirs, list)
                and wanted & {str(d).lower() for d in theirs}
            ):
                return str(p.get("name", ""))
        return None

    def _name_taken(self, name: object) -> bool:
        """Another profile has this name, by the same slug rule the file check applies."""
        assert self._document is not None
        key = slug(name) if isinstance(name, str) else ""
        return bool(key) and any(
            p["id"] != self._profile_id and slug(str(p.get("name", ""))) == key
            for p in self._document.data["profiles"]
        )

    def _profile_defaults(self) -> dict[str, Any]:
        profile = self._existing()
        names = {code: name for name, code in self._station_codes().items()}
        schedule: dict[str, Any] = {}
        for direction in ("outbound", "return"):
            journey = profile.get(direction, {})
            window = journey.get("window")  # fixed mode: an explicit watch window is kept
            window = window if isinstance(window, list) and len(window) == 2 else ["", ""]
            schedule |= {
                f"{direction}_mode": journey.get("mode", "fixed"),
                f"{direction}_window_start": window[0],
                f"{direction}_window_end": window[1],
                f"{direction}_pick": journey.get("pick", "earliest"),
                f"{direction}_min_stay": journey.get("min_stay_min", 0),
            }
        for direction in ("outbound", "return"):
            journey = profile.get(direction, {})
            if journey.get("mode") == "window" and "first_train" not in journey:
                schedule[f"{direction}_time"] = ""  # blank: a window day needs no first train
        return {
            "name": profile.get("name") or self._free_name(),
            "days": profile.get("days", ["mon", "tue", "wed", "thu", "fri"]),
            **{key: names.get(profile.get(key, "")) for key in _SINGLE_STATIONS},
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
                    ("active_lead", "active_lead_min", None),
                )
            },
            **schedule,  # last: a window profile's blank first train wins
        }

    def _profile_schema(self, defaults: Mapping[str, Any]) -> vol.Schema:
        options: list[SelectOptionDict] = [
            {"value": code, "label": label}
            for code, label in sorted(self._labels().items(), key=lambda item: item[1])
        ]
        # custom_value is what gives the single picker its search box; unknown text is refused
        station = SelectSelector(
            SelectSelectorConfig(options=sorted(self._station_codes()), custom_value=True)
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
                    translation_key="weekdays",
                )
            ),
            vol.Required("skip_public_holidays"): BooleanSelector(),
            vol.Required("require_accessible"): BooleanSelector(),
            vol.Required("disabled_categories"): SelectSelector(
                SelectSelectorConfig(
                    options=categories,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="alert_categories",
                )
            ),
        }
        for direction in ("outbound", "return"):
            fields[vol.Required(f"{direction}_transfers")] = transfers
            fields[vol.Required(f"{direction}_buffer")] = NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            )
        # Suggested values also preserve an intentionally cleared input after a validation error.
        schema: dict[Any, Any] = {}
        for key, value in fields.items():
            if (name := str(key)) in _SINGLE_STATIONS:
                # optional with no default: HA fills an empty required pick list with its
                # first station; the step itself refuses a missing station
                saved = defaults.get(name)
                schema[
                    vol.Optional(name, description={"suggested_value": saved} if saved else None)
                ] = value
            elif name in defaults:
                schema[vol.Required(name, description={"suggested_value": defaults[name]})] = value
        for direction in ("outbound", "return"):
            key = f"{direction}_active_lead"
            saved = defaults.get(key)
            schema[
                vol.Optional(
                    key, description={"suggested_value": saved} if saved is not None else None
                )
            ] = NumberSelector(NumberSelectorConfig(min=0, step=1, mode=NumberSelectorMode.BOX))
            # blank text comes back as no key: a window day needs no first train
            for name in _OPTIONAL_TEXT:
                key = f"{direction}_{name}"
                schema[vol.Optional(key, description={"suggested_value": defaults.get(key)})] = str
            choices = (("mode", MODE_OPTIONS, "fixed"), ("pick", PICK_OPTIONS, "earliest"))
            for name, options, default in choices:
                key = f"{direction}_{name}"
                schema[vol.Optional(key, default=defaults.get(key, default))] = SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=f"journey_{name}",
                    )
                )
            key = f"{direction}_min_stay"
            schema[vol.Optional(key, default=defaults.get(key, 0))] = NumberSelector(
                NumberSelectorConfig(min=0, max=1440, step=1, mode=NumberSelectorMode.BOX)
            )
        return vol.Schema(schema)

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
        other = ""
        if user_input is not None:
            # the form keeps names; everything after this step works with codes
            coded_values = {
                **values,
                **{
                    f"{d}_active_lead": values.get(f"{d}_active_lead")
                    for d in ("outbound", "return")
                },
                **{k: self._station_code(values.get(k)) for k in _SINGLE_STATIONS},
            }
            errors = {k: "unknown_station" for k in _SINGLE_STATIONS if coded_values[k] is None}
            if not errors and coded_values["home_station"] == coded_values["work_station"]:
                errors["work_station"] = "same_station"
            if (clash := self._day_clash(values.get("days"))) is not None:
                errors["days"], other = "days_clash", clash
            if self._name_taken(values.get("name")):
                errors["name"] = "name_taken"
            if not errors:
                try:
                    self._pending_profile = edit_profile(
                        self._document,
                        self._profile_id,
                        coded_values,
                        self._catalogue,
                        datetime.now(TZ).date(),
                    )
                except EditorError as err:
                    errors["base"], report = err.code, err.report
                else:
                    self._editor_values = coded_values
                    return await self.async_step_profile_details()
        return self.async_show_form(
            step_id="profile",
            data_schema=self._profile_schema(values),
            errors=errors,
            description_placeholders={"report": report, "other": other},
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
        existing = self._existing()
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
                    self._catalogue,
                    datetime.now(TZ).date(),
                    available_channels=self._profile_channels(),
                )
            except EditorError as err:
                errors["base"], report = err.code, err.report
            else:
                self._editor_values = details
                if user_input.get("edit_weekday_times", False):
                    return await self.async_step_profile_weekdays()
                return await self._after_weekdays()
        fields: dict[Any, Any] = {}
        for key in ("edit_weekday_times", "edit_day_overrides"):
            fields[vol.Optional(key, default=values.get(key, False))] = BooleanSelector()
        for key in ("delay_min", "delay_step_min"):
            fields[vol.Optional(
                key, default=values.get(key, existing.get("alerts", {}).get(key, 3))
            )] = NumberSelector(
                NumberSelectorConfig(min=1, max=1440, step=1, mode=NumberSelectorMode.BOX)
            )
        for key in ("compensation_reminder", "quiet_repeat_misses", "leave_by"):
            fields[
                vol.Optional(
                    key,
                    default=values.get(key, existing.get("alerts", {}).get(key, key == "leave_by")),
                )
            ] = BooleanSelector()
        location = existing.get("location")
        if isinstance(location, dict):  # other boarding stations need a location source
            fields.update(self._other_station_fields(existing, location, values))
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
                    translation_key="holiday_ranges",
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
            if selected == "primary,webhook":
                selected = "primary_webhook"
            choices: list[SelectOptionDict] = [
                {"value": "default", "label": "All configured destinations"},
                {"value": "primary", "label": "Phone"},
            ]
            if "webhook" in configured:
                choices += [
                    {"value": "webhook", "label": "Webhooks"},
                    {"value": "primary_webhook", "label": "Phone and webhooks"},
                ]
            if selected not in {choice["value"] for choice in choices}:
                choices.append(
                    {"value": selected, "label": "Unavailable destinations: " + selected}
                )
            fields[vol.Optional(key, default=selected)] = SelectSelector(
                SelectSelectorConfig(
                    options=choices,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="notification_destinations",
                )
            )
        labels = (
            ", ".join(f"minimum_{code}: {self._catalogue.name(code)}" for code in stations)
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

    def _other_station_fields(
        self, existing: Mapping[str, Any], location: Mapping[str, Any], values: Mapping[str, Any]
    ) -> dict[Any, Any]:
        options: list[SelectOptionDict] = [
            {"value": code, "label": label}
            for code, label in sorted(self._labels().items(), key=lambda item: item[1])
        ]
        fields: dict[Any, Any] = {}
        for direction in ("outbound", "return"):
            key = f"{direction}_other_stations"
            saved = existing.get(direction, {}).get("other_stations", [])
            fields[vol.Optional(key, default=values.get(key, saved))] = SelectSelector(
                SelectSelectorConfig(
                    options=options, mode=SelectSelectorMode.DROPDOWN, multiple=True
                )
            )
        for key, high in (("other_station_radius_m", 5000), ("other_station_dwell_min", 60)):
            default = values.get(key, location.get(key, OTHER_STATION_DEFAULTS[key]))
            low = 1 if key == "other_station_radius_m" else 0
            fields[vol.Optional(key, default=default)] = NumberSelector(
                NumberSelectorConfig(min=low, max=high, step=1, mode=NumberSelectorMode.BOX)
            )
        key = "ask_to_remember_station"
        default = values.get(key, location.get(key, True))
        fields[vol.Optional(key, default=default)] = BooleanSelector()
        return fields

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
                    self._catalogue,
                    datetime.now(TZ).date(),
                    available_channels=self._profile_channels(),
                )
            except EditorError as err:
                errors["base"], report = err.code, err.report
            else:
                self._editor_values = details
                return await self._after_weekdays()
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
                "outbound_default": self._editor_values.get("outbound_time") or "none",
                "return_default": self._editor_values.get("return_time") or "none",
                "report": report,
            },
        )

    async def _after_weekdays(self) -> ConfigFlowResult:
        assert self._editor_values is not None
        if self._editor_values.get("edit_day_overrides", False):
            existing: dict[str, Any] = (
                next(
                    (p for p in self._document.data["profiles"] if p["id"] == self._profile_id), {}
                )
                if self._document is not None
                else {}
            )
            for direction in ("outbound", "return"):
                key = f"{direction}_by_weekday"
                if key not in self._editor_values:
                    saved = existing.get(direction, {}).get("by_weekday", {})
                    self._editor_values[key] = dict(saved) if isinstance(saved, dict) else {}
            return await self.async_step_profile_day_pick()
        self._profile_phase = ReviewPhase.REVIEWING
        return await self._profile_ready()

    def _day_summary(self) -> str:
        assert self._editor_values is not None
        lines = [
            f"{direction} {DAY_NAMES[day]}: " + ", ".join(f"{k} {v}" for k, v in override.items())
            for direction in ("outbound", "return")
            for day, override in self._editor_values.get(f"{direction}_by_weekday", {}).items()
            if day in DAY_NAMES
        ]
        return "; ".join(lines) or "none"

    async def async_step_profile_day_pick(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Day overrides, one day at a time: pick a day, edit it, come back, or finish."""
        if self._profile_phase is not ReviewPhase.EDITING or self._editor_values is None:
            return self._editor_error("invalid_profile")
        if user_input is not None:
            target = user_input.get("day", "done")
            if target == "done":
                self._profile_phase = ReviewPhase.REVIEWING
                return await self._profile_ready()
            direction, _, day = str(target).replace(":", "_").partition("_")
            if direction not in ("outbound", "return") or day not in DAY_NAMES:
                return self._editor_error("invalid_profile")
            self._day_target = (direction, day)
            return await self.async_step_profile_day()
        options: list[SelectOptionDict] = [{"value": "done", "label": "Done"}]
        options += [
            {"value": f"{direction}_{day}", "label": f"{DAY_NAMES[day]} {direction}"}
            for direction in ("outbound", "return")
            for day in WEEKDAYS
        ]
        return self.async_show_form(
            step_id="profile_day_pick",
            data_schema=vol.Schema(
                {
                    vol.Required("day", default="done"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="profile_day",
                        )
                    )
                }
            ),
            description_placeholders={"overrides": self._day_summary()},
        )

    async def async_step_profile_day(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (
            self._profile_phase is not ReviewPhase.EDITING
            or self._document is None
            or self._editor_values is None
            or self._catalogue is None
            or self._day_target is None
        ):
            return self._editor_error("invalid_profile")
        direction, day = self._day_target
        key = f"{direction}_by_weekday"
        saved: dict[str, Any] = dict(self._editor_values[key].get(day, {}))
        errors: dict[str, str] = {}
        report = ""
        if user_input is not None:
            override: dict[str, Any] = {}
            for name in ("mode", "pick"):
                if user_input.get(name, "default") != "default":
                    override[name] = user_input[name]
            start, end = (str(user_input.get(f"window_{e}", "")).strip() for e in ("start", "end"))
            if start or end:
                override["window"] = [start, end]
            stay = user_input.get("min_stay")
            if isinstance(stay, float) and stay.is_integer():
                stay = int(stay)
            if stay is not None:  # 0 turns off the stay this day would inherit
                override["min_stay_min"] = stay
            days = {k: v for k, v in self._editor_values[key].items() if k != day}
            if override:
                days[day] = override
            details = {**self._editor_values, key: days}
            try:
                self._pending_profile = edit_profile(
                    self._document,
                    self._profile_id,
                    details,
                    self._catalogue,
                    datetime.now(TZ).date(),
                    available_channels=self._profile_channels(),
                )
            except EditorError as err:
                errors["base"], report = err.code, err.report
                saved = override
            else:
                self._editor_values = details
                return await self.async_step_profile_day_pick()
        window = saved.get("window")
        window = window if isinstance(window, list) and len(window) == 2 else ["", ""]
        default: list[SelectOptionDict] = [{"value": "default", "label": "Same as every day"}]
        fields: dict[Any, Any] = {
            vol.Optional("mode", default=saved.get("mode", "default")): SelectSelector(
                SelectSelectorConfig(
                    options=default + MODE_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="journey_mode",
                )
            ),
            vol.Optional("window_start", description={"suggested_value": window[0]}): str,
            vol.Optional("window_end", description={"suggested_value": window[1]}): str,
            vol.Optional("pick", default=saved.get("pick", "default")): SelectSelector(
                SelectSelectorConfig(
                    options=default + PICK_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="journey_pick",
                )
            ),
            vol.Optional(
                "min_stay", description={"suggested_value": saved.get("min_stay_min")}
            ): NumberSelector(
                NumberSelectorConfig(min=0, max=1440, step=1, mode=NumberSelectorMode.BOX)
            ),
        }
        return self.async_show_form(
            step_id="profile_day",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "day": f"{DAY_NAMES[day]} {direction}",
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
        known, roots = self._catalogue, self._roots()
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

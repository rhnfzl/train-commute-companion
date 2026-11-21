"""Native profile management using the shared portable profile editor."""

import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from train_commute_companion.calendar import TZ
from train_commute_companion.model import NotificationChannel, Profile, Stations
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.state import PollFailure

from .const import CONF_PROFILES_PATH, CONF_WEBHOOK_URLS, DOMAIN
from .data import TccConfigEntry
from .host import RuntimeNotRunning
from .profile_editor import EditorError, read_document, save_document, validate_text
from .profile_flow import ProfileEditorFlow

PROFILE_SUBENTRY = "profile"
_MANAGED_IDS = "managed_profile_ids"
_LOGGER = logging.getLogger(__name__)


def profile_subentries(entry: ConfigEntry, path: str | None = None) -> dict[str, ConfigSubentry]:
    return {
        subentry.unique_id: subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == PROFILE_SUBENTRY
        and subentry.unique_id is not None
        and (path is None or subentry.data.get("source_path") == path)
    }


def _managed(entry: ConfigEntry, path: str) -> frozenset[str]:
    return frozenset(entry.data.get(_MANAGED_IDS, {}).get(path, ()))


def excluded_profile_ids(entry: ConfigEntry, path: str) -> frozenset[str]:
    return _managed(entry, path) - profile_subentries(entry, path).keys()


@callback
def _save_managed(entry: TccConfigEntry, path: str, ids: set[str] | frozenset[str]) -> None:
    if ids != _managed(entry, path):
        paths = {**entry.data.get(_MANAGED_IDS, {}), path: sorted(ids)}
        entry.runtime_data.coordinator.hass.config_entries.async_update_entry(
            entry, data={**entry.data, _MANAGED_IDS: paths}
        )


@callback
def sync_profile_subentries(entry: TccConfigEntry, path: str, profiles: Sequence[Profile]) -> None:
    hass = entry.runtime_data.coordinator.hass
    entries = hass.config_entries
    devices, entities = dr.async_get(hass), er.async_get(hass)
    current = profile_subentries(entry)
    known = set(_managed(entry, path))
    excluded = excluded_profile_ids(entry, path)
    for profile in profiles:
        if profile.travel_plan or profile.id in excluded:
            continue
        subentry = current.get(profile.id)
        if subentry is None:
            subentry = ConfigSubentry(
                data=MappingProxyType({"profile_id": profile.id, "source_path": path}),
                subentry_type=PROFILE_SUBENTRY,
                title=profile.name,
                unique_id=profile.id,
            )
            entries.async_add_subentry(entry, subentry)
        else:
            previous_path = subentry.data.get("source_path")
            if previous_path != path and isinstance(previous_path, str):
                _save_managed(entry, previous_path, _managed(entry, previous_path) - {profile.id})
            entries.async_update_subentry(
                entry,
                subentry,
                title=profile.name,
                data={"profile_id": profile.id, "source_path": path},
            )
        device = devices.async_get_device_by_identifier((DOMAIN, profile.id), entry.entry_id)
        if device is not None and device.config_subentry_id != subentry.subentry_id:
            devices.async_update_device(device.id, new_config_subentry_id=subentry.subentry_id)
            for entity in er.async_entries_for_device(
                entities, device.id, include_disabled_entities=True
            ):
                if entity.config_entry_id == entry.entry_id:
                    entities.async_update_entity(
                        entity.entity_id, config_subentry_id=subentry.subentry_id
                    )
        known.add(profile.id)
    _save_managed(entry, path, known)


def _remove_from_yaml(
    path: Path, roots: tuple[Path, ...], known: frozenset[str], excluded: frozenset[str]
) -> None:
    today = datetime.now(TZ).date()
    document = read_document(path, roots, known, today, allow_empty=True)
    rows = document.data["profiles"]
    active = [row for row in rows if row["id"] not in excluded]
    if active == rows:
        return
    text = yaml.safe_dump(
        {**document.data, "profiles": active}, sort_keys=False, allow_unicode=True
    )
    validate_text(text, known, today, allow_empty=True)
    save_document(document, text, roots)


@callback
def async_setup_profile_subentries(entry: TccConfigEntry, profiles_path: str) -> None:
    data = entry.runtime_data
    hass = data.coordinator.hass
    removal = asyncio.Lock()
    observed = set(profile_subentries(entry))
    restore_pending = bool(excluded_profile_ids(entry, profiles_path))
    pruning_yaml = False

    async def apply_removals() -> None:
        async with removal:
            excluded = excluded_profile_ids(entry, profiles_path)
            if not excluded:
                return
            try:
                if await data.host.run(lambda rt: rt.reload()) is not None:
                    return
                known = await data.host.run(lambda rt: rt.known_stations)
            except RuntimeNotRunning:
                return
            if known:
                roots = (
                    Path(hass.config.config_dir),
                    *map(Path, hass.config.allowlist_external_dirs),
                )
                try:
                    await hass.async_add_executor_job(
                        _remove_from_yaml, Path(profiles_path), roots, known, excluded
                    )
                except EditorError as err:
                    _LOGGER.warning(
                        "Removed profile is disabled but its YAML could not be updated (%s)",
                        err.code,
                    )
                else:
                    _save_managed(entry, profiles_path, _managed(entry, profiles_path) - excluded)
            await data.coordinator.async_request_refresh()

    @callback
    def update() -> None:
        nonlocal pruning_yaml, restore_pending
        view = data.coordinator.data
        sync_profile_subentries(entry, profiles_path, view.profiles)
        if view.started and restore_pending:
            restore_pending = False
            entry.async_create_background_task(hass, apply_removals(), "Restore profile removals")
        if view.started and view.store_ok and view.refusal is None:
            configured = {p.id for p in view.profiles if not p.travel_plan}
            retained = {d.profile_id for d in view.directions}
            current = profile_subentries(entry)
            for profile_id in observed - configured - retained:
                if (subentry := current.get(profile_id)) is None:
                    continue
                # Home Assistant dispatches entry updates inside both calls.
                pruning_yaml = True
                try:
                    if isinstance(source := subentry.data.get("source_path"), str):
                        _save_managed(entry, source, _managed(entry, source) - {profile_id})
                    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
                finally:
                    pruning_yaml = False
            observed.intersection_update(profile_subentries(entry))
            observed.update(configured)

    @callback
    def changed(change: ConfigEntryChange, edited: ConfigEntry) -> None:
        nonlocal restore_pending
        if pruning_yaml or change is not ConfigEntryChange.UPDATED or edited is not entry:
            return
        known = _managed(entry, profiles_path)
        present = profile_subentries(entry, profiles_path).keys()
        _save_managed(entry, profiles_path, known | present)
        if excluded_profile_ids(entry, profiles_path):
            if data.coordinator.data.started:
                entry.async_create_background_task(
                    hass, apply_removals(), "Remove commute profiles"
                )
            else:
                restore_pending = True

    update()
    entry.async_on_unload(data.coordinator.async_add_listener(update))
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, changed))


class ProfileSubentryFlow(ProfileEditorFlow[SubentryFlowResult], ConfigSubentryFlow):
    _selected: str | None = None

    @property
    def _parent(self) -> TccConfigEntry:
        return self._get_entry()

    async def _open(self) -> SubentryFlowResult:
        parent = self._parent
        runtime = getattr(parent, "runtime_data", None)
        if runtime is None:
            return self.async_abort(reason="not_loaded")
        try:
            async with asyncio.timeout(10):
                known = await runtime.host.run(lambda rt: rt.known_stations or rt.stations())
        except (TimeoutError, RuntimeNotRunning):
            known = None
        if not known:
            return self._editor_error("cannot_connect")
        catalogue = known if isinstance(known, Stations) else Stations(dict.fromkeys(known, ()))
        default = self.hass.config.path(DOMAIN, "profiles.yaml")
        path = parent.options.get(CONF_PROFILES_PATH, default)
        create = path == default and not await self.hass.async_add_executor_job(
            os.path.lexists, path
        )
        result = await self._begin_editor(
            parent.data[CONF_API_KEY],
            path,
            create=create and self._selected is None,
            catalogue=catalogue,
        )
        if result.get("step_id") == "profiles":
            selected = "new" if self._selected is None else f"edit:{self._selected}"
            result = await self.async_step_profiles({"profile": selected})
        return result

    async def async_step_user(self, _: Mapping[str, Any] | None = None) -> SubentryFlowResult:
        return await self._open()

    async def async_step_reconfigure(
        self, _: Mapping[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._selected = self._get_reconfigure_subentry().unique_id
        return await self._open()

    async def async_step_profile_error(
        self, user_input: Mapping[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._open()

    async def _preview_trips(self, query: Query) -> ProviderAnswer | PollFailure | None:
        parent = self._parent
        if hasattr(parent, "runtime_data"):
            try:
                return await parent.runtime_data.host.run(lambda rt: rt.preview_trip(query))
            except RuntimeNotRunning:
                pass
        return None

    def _profile_channels(self) -> frozenset[NotificationChannel]:
        return (
            frozenset({"primary", "webhook"})
            if self._parent.options.get(CONF_WEBHOOK_URLS)
            else frozenset({"primary"})
        )

    async def _profile_ready(self) -> SubentryFlowResult:
        try:
            await self._save_profile()
        except EditorError as err:
            return self._editor_error(err.code, err.report)
        assert self._pending_profile is not None
        rows = yaml.safe_load(self._pending_profile)["profiles"]
        if self._selected is None:
            row = rows[-1]
            parent = self._parent
            if row["id"] in profile_subentries(parent):
                return self.async_abort(reason="profile_saved")
            result = self.async_create_entry(
                title=row["name"],
                data={"profile_id": row["id"], "source_path": self._editor_path},
                unique_id=row["id"],
            )
        else:
            row = next(row for row in rows if row["id"] == self._selected)
            result = self.async_update_and_abort(
                self._parent, self._get_reconfigure_subentry(), title=row["name"]
            )
        return result

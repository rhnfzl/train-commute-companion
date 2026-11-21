"""Train Commute Companion: the core runtime on its own thread, read through a coordinator."""

import os
import threading
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

import httpx
from homeassistant.const import CONF_API_KEY, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from train_commute_companion.providers.weather import WeatherProvider
from train_commute_companion.runtime import Runtime
from train_commute_companion.store import JsonFileStore
from train_commute_companion.webhook_send import WebhookPoster

from . import host as host_mod
from .const import (
    CONF_KNMI_API_KEY,
    CONF_LOUD_STYLE,
    CONF_NOTIFICATIONS_ENABLED_ENTITY,
    CONF_NOTIFICATIONS_QUIET_ENTITY,
    CONF_NOTIFY,
    CONF_PROFILES_PATH,
    CONF_REMOTE_URL,
    CONF_STARTED,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_TOKEN,
    CONF_WEBHOOK_URLS,
    DEFAULT_LOUD_STYLE,
    DOMAIN,
    PLATFORMS,
    LoudStyle,
)
from .coordinator import TccCoordinator
from .data import TccConfigEntry, TccData
from .location import LocationTracker
from .notifier import HaNotifier
from .profile_subentries import async_setup_profile_subentries, excluded_profile_ids
from .remote import RemoteRuntime, open_remote
from .services import async_setup_services
from .store import EntryMarkedStore, file_lock

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    if hass.http is not None:
        from .remote_alert import RemoteAlertView

        hass.http.register_view(RemoteAlertView(hass))
    return True


def _closing_client(
    close_provider: Callable[[], Awaitable[None]], client: httpx.AsyncClient
) -> Callable[[], Awaitable[None]]:
    async def close() -> None:
        try:
            await close_provider()
        finally:
            await client.aclose()

    return close


async def async_setup_entry(hass: HomeAssistant, entry: TccConfigEntry) -> bool:
    notify_service: str = entry.options[CONF_NOTIFY]
    loud_style: LoudStyle = entry.options.get(CONF_LOUD_STYLE, DEFAULT_LOUD_STYLE)
    entry_id = entry.entry_id
    retired = threading.Event()
    connected = bool(entry.options.get(CONF_REMOTE_URL))
    profiles_path: str = entry.options.get(
        CONF_PROFILES_PATH, hass.config.path(DOMAIN, "profiles.yaml")
    )
    notifier = HaNotifier(
        hass,
        entry_id,
        notify_service,
        loud_style,
        retired,
        enabled_entity=entry.options.get(CONF_NOTIFICATIONS_ENABLED_ENTITY),
        quiet_entity=entry.options.get(CONF_NOTIFICATIONS_QUIET_ENTITY),
        profile_allowed=None
        if connected
        else lambda pid: pid not in excluded_profile_ids(entry, profiles_path),
        timetable_actions=not connected,
    )
    if connected:
        try:
            remote = await open_remote(hass, entry.options)
        except (OSError, ValueError) as err:
            raise ConfigEntryNotReady("The Docker connection could not be opened.") from err
        return await _setup_platforms(hass, entry, remote, notifier)

    await hass.async_add_executor_job(partial(os.makedirs, hass.config.path(DOMAIN), exist_ok=True))
    # plain values read here on hass.loop; build runs on the runtime thread and touches only these
    state_path = Path(hass.config.path(DOMAIN, f"state-{entry.entry_id}.json"))
    api_key: str = entry.data[CONF_API_KEY]
    knmi_key: str | None = entry.options.get(CONF_KNMI_API_KEY) or None
    started = bool(entry.data.get(CONF_STARTED, False))
    webhook_urls = tuple(entry.options.get(CONF_WEBHOOK_URLS, ()))
    webhook_secret: str = entry.options.get(CONF_WEBHOOK_SECRET, "")
    webhook_token: str = entry.options.get(CONF_WEBHOOK_TOKEN, "")

    @callback
    def set_started() -> None:
        if hass.config_entries.async_get_entry(entry_id) is not None:
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_STARTED: True})

    def mark() -> None:
        # no update listener exists, so this edit never reloads the entry
        hass.loop.call_soon_threadsafe(set_started)

    lock = file_lock(state_path)

    def build() -> host_mod.Parts:
        clock = host_mod.make_clock()
        provider, close = host_mod.make_provider(api_key, clock)
        weather_client = host_mod.make_weather_client()
        close = _closing_client(close, weather_client)
        webhooks = None
        if webhook_urls:
            client = host_mod.make_webhook_client()
            webhooks = WebhookPoster(
                client,
                webhook_urls,
                webhook_secret,
                clock.now,
                token=webhook_token,
                stopped=retired.is_set,
            )
            close = _closing_client(close, client)

        rt = Runtime(
            profile_path=Path(profiles_path),
            store=EntryMarkedStore(JsonFileStore(state_path), started, mark, retired, lock),
            provider=provider,
            notifier=notifier,
            clock=clock,
            require_stations=True,
            webhooks=webhooks,
            primary_destination=lambda: notifier.destination_id,
            wake_destination=lambda: notifier.wake_destination,
            wake_ready=notifier.wake_ready,
            send_partner_eta=notifier.send_partner_eta,
            excluded_profiles=lambda: excluded_profile_ids(entry, profiles_path),
            weather=WeatherProvider(weather_client, clock.now, knmi_key),
        )
        return host_mod.Parts(rt, clock, close)

    host = host_mod.RuntimeHost(build, retired=retired)
    return await _setup_platforms(hass, entry, host, notifier, profiles_path)


async def _setup_platforms(
    hass: HomeAssistant,
    entry: TccConfigEntry,
    host: host_mod.RuntimeHost | RemoteRuntime,
    notifier: HaNotifier,
    profiles_path: str | None = None,
) -> bool:
    tracker: LocationTracker | None = None

    async def stop() -> None:
        if tracker is not None:
            await tracker.stop()  # before the host, so no event is sent to a retired runtime
        notifier.retired.set()
        await notifier.async_stop()
        await host.stop()

    # one guard from the first await on, so a cancelled setup also stops the thread it started
    try:
        if isinstance(host, host_mod.RuntimeHost):
            try:
                await host.start()
            except Exception as err:
                raise ConfigEntryNotReady(f"the runtime did not start: {err}") from err

        coordinator = TccCoordinator(
            hass,
            entry,
            host,
            lambda: notifier.available,
            notification_paused=lambda: notifier.paused,
            notification_quiet=lambda: notifier.quiet,
        )
        await coordinator.async_config_entry_first_refresh()
        # HA polls a coordinator only while it has a listener; monitoring must not wait for entities
        entry.async_on_unload(coordinator.async_add_listener(lambda: None))
        tracker = LocationTracker(hass, entry, coordinator, host)
        entry.runtime_data = TccData(host, coordinator, stop, tracker, notifier)
        from .arrival_controls import listen_for_arrival
        from .timetable_controls import listen_for_timetable

        listen_for_arrival(entry.runtime_data, notifier)
        if profiles_path is not None:
            async_setup_profile_subentries(entry, profiles_path)
            listen_for_timetable(entry.runtime_data, notifier, Path(profiles_path))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        tracker.start()

        async def on_stop(_: Event) -> None:
            # HA does not unload entries when it stops; without this the thread keeps writing
            await stop()

        # async_listen, not _once: removing a fired one-time listener on unload logs an error
        entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, on_stop))
    except BaseException:
        await stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TccConfigEntry) -> bool:
    tracker = entry.runtime_data.tracker
    await tracker.stop()
    try:
        ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except BaseException:
        tracker.start()  # the entry stays loaded, so it keeps listening
        raise
    if ok:
        await entry.runtime_data.stop()
    else:
        tracker.start()
    return ok

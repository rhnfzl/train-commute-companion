"""Train Commute Companion: the core runtime on its own thread, read through a coordinator."""

import os
from functools import partial
from pathlib import Path

from homeassistant.const import CONF_API_KEY, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from train_commute_companion.runtime import Runtime, WebhookPoster
from train_commute_companion.store import JsonFileStore

from . import host as host_mod
from .const import (
    CONF_LOUD_STYLE,
    CONF_NOTIFY,
    CONF_PROFILES_PATH,
    CONF_STARTED,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_TOKEN,
    CONF_WEBHOOK_URLS,
    DEFAULT_LOUD_STYLE,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import TccCoordinator
from .data import TccConfigEntry, TccData
from .location import LocationTracker
from .notifier import HaNotifier
from .services import async_setup_services
from .store import EntryMarkedStore, file_lock

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TccConfigEntry) -> bool:
    await hass.async_add_executor_job(partial(os.makedirs, hass.config.path(DOMAIN), exist_ok=True))
    # plain values read here on hass.loop; build runs on the runtime thread and touches only these
    state_path = Path(hass.config.path(DOMAIN, f"state-{entry.entry_id}.json"))
    api_key: str = entry.data[CONF_API_KEY]
    started = bool(entry.data.get(CONF_STARTED, False))
    profiles_path: str = entry.options.get(
        CONF_PROFILES_PATH, hass.config.path(DOMAIN, "profiles.yaml")
    )
    notify_service: str = entry.options[CONF_NOTIFY]
    loud_style: str = entry.options.get(CONF_LOUD_STYLE, DEFAULT_LOUD_STYLE)
    webhook_urls = tuple(entry.options.get(CONF_WEBHOOK_URLS, ()))
    webhook_secret: str = entry.options.get(CONF_WEBHOOK_SECRET, "")
    webhook_token: str = entry.options.get(CONF_WEBHOOK_TOKEN, "")
    entry_id = entry.entry_id

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
        webhooks = None
        if webhook_urls:
            client = host_mod.make_webhook_client()
            webhooks = WebhookPoster(
                client,
                webhook_urls,
                webhook_secret,
                clock.now,
                token=webhook_token,
                stopped=host.retired.is_set,
            )
            close_provider = close

            async def close() -> None:
                try:
                    await close_provider()
                finally:
                    await client.aclose()

        rt = Runtime(
            profile_path=Path(profiles_path),
            store=EntryMarkedStore(JsonFileStore(state_path), started, mark, host.retired, lock),
            provider=provider,
            notifier=notifier,
            clock=clock,
            known_stations=None,
            webhooks=webhooks,
        )
        rt.require_stations = True
        return host_mod.Parts(rt, clock, provider, close)

    host = host_mod.RuntimeHost(build, autorun=host_mod.AUTORUN)
    # built here on hass.loop: an HA object, and unload needs it to cancel its pending pushes
    notifier = HaNotifier(hass, entry_id, notify_service, loud_style, host.retired)

    tracker: LocationTracker | None = None

    async def stop() -> None:
        if tracker is not None:
            await tracker.stop()  # before the host, so no event is sent to a retired runtime
        host.retired.set()  # first, so no push or write starts while the thread is being stopped
        await notifier.async_stop()
        await host.stop()

    # one guard from the first await on, so a cancelled setup also stops the thread it started
    try:
        try:
            await host.start()
        except Exception as err:
            raise ConfigEntryNotReady(f"the runtime did not start: {err}") from err

        coordinator = TccCoordinator(hass, entry, host)
        await coordinator.async_config_entry_first_refresh()
        # HA polls a coordinator only while it has a listener; monitoring must not wait for entities
        entry.async_on_unload(coordinator.async_add_listener(lambda: None))
        entry.runtime_data = TccData(host, coordinator, stop)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        tracker = entry.runtime_data.tracker = LocationTracker(hass, entry)
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
    if tracker is not None:
        await tracker.stop()
    try:
        ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except BaseException:
        if tracker is not None:
            tracker.start()  # the entry stays loaded, so it keeps listening
        raise
    if ok:
        await entry.runtime_data.stop()
    elif tracker is not None:
        tracker.start()
    return ok

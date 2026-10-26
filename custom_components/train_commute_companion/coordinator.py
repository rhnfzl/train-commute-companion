"""Reads the runtime's view every REFRESH_S seconds and keeps the repair issues in step with it."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from train_commute_companion.view import RuntimeView

from .const import CONF_NOTIFY, DOMAIN, REFRESH_S
from .host import RuntimeHost

_LOGGER = logging.getLogger(__name__)

RUN_TIMEOUT_S = 10
RELOAD_AFTER_TIMEOUTS = 4
MOVED_PREFIX = "moved_"


class TccCoordinator(DataUpdateCoordinator[RuntimeView]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, host: RuntimeHost) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=REFRESH_S),
        )
        self.host = host
        self._timeouts = 0

    def _reload(self) -> None:
        assert self.config_entry is not None
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _async_update_data(self) -> RuntimeView:
        if not self.host.alive:
            self._reload()
            raise UpdateFailed("runtime stopped; reloading")
        try:
            # a runtime loop stuck in a synchronous call stays alive but never answers (rule 12)
            async with asyncio.timeout(RUN_TIMEOUT_S):
                view = await self.host.run(lambda rt: rt.view())
        except TimeoutError:
            self._timeouts += 1
            if self._timeouts == RELOAD_AFTER_TIMEOUTS:
                self._reload()
            raise UpdateFailed("runtime not answering") from None
        self._timeouts = 0
        self._sync_issues(view)
        return view

    def _sync_issues(self, view: RuntimeView) -> None:
        """Repairs come only from the view, so each one clears itself once the view does."""
        hass = self.hass
        if view.refusal is not None:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "profile_refused",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="profile_refused",
                translation_placeholders={"reason": view.refusal},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, "profile_refused")

        moved = {f"{MOVED_PREFIX}{m.profile_id}_{m.direction.value}": m.text for m in view.moved}
        for issue_id, text in moved.items():
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="moved",
                translation_placeholders={"message": text},
            )
        stale = [
            i
            for (domain, i) in ir.async_get(hass).issues
            if domain == DOMAIN and i.startswith(MOVED_PREFIX) and i not in moved
        ]
        for issue_id in stale:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

        if view.undelivered > 0:
            assert self.config_entry is not None
            ir.async_create_issue(
                hass,
                DOMAIN,
                "notify_failing",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="notify_failing",
                translation_placeholders={"service": self.config_entry.options[CONF_NOTIFY]},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, "notify_failing")

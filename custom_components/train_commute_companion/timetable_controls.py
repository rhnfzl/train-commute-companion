"""Apply a still-current adoption from the configured mobile companion."""

import logging
from pathlib import Path

from homeassistant.core import Event

from train_commute_companion.calendar import TZ
from train_commute_companion.messages import Refused
from train_commute_companion.runtime import Runtime
from train_commute_companion.timetable_update import resolve_edit

from .data import TccData
from .notifier import TIMETABLE_PREFIX, HaNotifier
from .profile_editor import EditorError, edit_timetable, read_document, save_document
from .services import MOBILE_ACTION, _call_runtime

_LOGGER = logging.getLogger(__name__)


async def apply_timetable(
    runtime: Runtime, path: Path, roots: tuple[Path, ...], token: str, recipient: str
) -> Refused | None:
    edit = resolve_edit(runtime.state, token, recipient)
    cfg = runtime.config
    if edit is None or cfg is None:
        return Refused("This timetable update has expired. Check the profile editor.")
    known = runtime.known_stations
    if not runtime.store_ok or known is None:
        return Refused("The companion cannot save the timetable now. Try again.")
    today = runtime.now().astimezone(TZ).date()
    try:
        # Verify the live adoption and compare-and-save on the same runtime turn.
        document = read_document(path, roots, known, today)
        text = edit_timetable(document, edit, cfg, known, today, runtime.available_channels)
        save_document(document, text, roots)
    except EditorError:
        return Refused("The timetable update could not be saved. Check the profile editor.")
    return await runtime.reload(just_saved=text)


def listen_for_timetable(data: TccData, notifier: HaNotifier, path: Path) -> None:
    hass = notifier.hass

    async def answer(event: Event) -> None:
        action = event.data.get("action")
        if not isinstance(action, str) or not action.startswith(TIMETABLE_PREFIX):
            return
        recipient = notifier.action_recipient(event)
        if recipient is None:
            return
        roots = (Path(hass.config.config_dir), *map(Path, hass.config.allowlist_external_dirs))
        refused = await _call_runtime(
            data,
            lambda rt: apply_timetable(
                rt, path, roots, action.removeprefix(TIMETABLE_PREFIX), recipient
            ),
        )
        if refused is not None:
            _LOGGER.info("timetable update from the phone declined: %s", refused.text)
        await data.coordinator.async_request_refresh()

    entry = data.coordinator.config_entry
    entry.async_on_unload(hass.bus.async_listen(MOBILE_ACTION, answer))

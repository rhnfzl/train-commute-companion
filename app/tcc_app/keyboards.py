import asyncio
import logging
from collections.abc import Callable, Mapping
from enum import Enum, auto
from typing import Any

from .telegram import TelegramApi, TelegramError

log = logging.getLogger(__name__)

_CLEAR_ON_SUCCESS = frozenset({"ok", "av", "ap", "q", "ra", "tp", "ta", "ag", "tm", "setup"})


def _retirement_key(data: str) -> str:
    if data.startswith("rc|"):
        selection_revision_step, _, _action = data.rpartition("|")
        return selection_revision_step
    return data


def _callback_data(button: Any) -> str | None:
    data = button.get("callback_data") if isinstance(button, dict) else None
    return data if isinstance(data, str) else None


class _Retirement(Enum):
    CLEARED = auto()


class TelegramKeyboards:
    """Remove used controls from a bound chat, even when taps carry stale keyboards."""

    def __init__(self, api: TelegramApi, closed: Callable[[], bool]) -> None:
        self._api = api
        self._closed = closed
        self._lock = asyncio.Lock()
        self._retired: dict[tuple[int, int], set[str] | _Retirement] = {}

    async def retire(self, chat: int, message: Mapping[str, Any], data: str) -> None:
        message_id = message.get("message_id")
        async with self._lock:
            if type(message_id) is not int or self._closed():
                log.info("no Telegram message to remove the used button from")
                return
            key = chat, message_id
            if data.partition("|")[0] in _CLEAR_ON_SUCCESS:
                self._retired[key] = _Retirement.CLEARED
            removed = self._retired.setdefault(key, set())
            rows: list[Any] = []
            if isinstance(removed, set):
                removed.add(_retirement_key(data))
                markup = message.get("reply_markup")
                old = markup.get("inline_keyboard") if isinstance(markup, dict) else None
                if not isinstance(old, list):
                    old = []
                rows = [
                    kept
                    for row in old
                    if isinstance(row, list)
                    and (
                        kept := [
                            button
                            for button in row
                            if (callback := _callback_data(button)) is None
                            or _retirement_key(callback) not in removed
                        ]
                    )
                ]
                if rows == old:
                    log.info("the used button is not on its Telegram message's keyboard")
                    return
            try:
                await self._api.call(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": chat,
                        "message_id": message_id,
                        "reply_markup": {"inline_keyboard": rows},
                    },
                )
            except TelegramError as error:
                log.info("Telegram keyboard removal failed (%s: %s)", error.code, error)

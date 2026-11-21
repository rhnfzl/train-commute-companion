"""Maintain one silent, read-only status pin per confirmed private Telegram destination."""

import hashlib
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .telegram import TEXT_LIMIT, TelegramApi, TelegramError, split_text

log = logging.getLogger(__name__)

INTERVAL_S = 15
AUDIT_INTERVAL = timedelta(minutes=5)
WITHDRAWN = "Pinned commute status is off."


@dataclass(frozen=True, slots=True)
class StatusPin:
    message_id: int | None
    digest: str | None
    pinned: bool = False
    retry_until: int = 0


def load_pins(raw: object) -> dict[str, StatusPin]:
    if not isinstance(raw, dict):
        raise TypeError("invalid status pins")
    pins = {}
    for binding, data in raw.items():
        if not re.fullmatch(r"[0-9]{1,20}:[1-9][0-9]{0,19}", binding) or not isinstance(data, dict):
            raise ValueError("invalid status binding")
        pin = StatusPin(**data)
        if (
            (
                pin.message_id is not None
                and (type(pin.message_id) is not int or pin.message_id <= 0)
            )
            or (
                pin.digest is not None
                and (
                    not isinstance(pin.digest, str) or not re.fullmatch(r"[a-f0-9]{64}", pin.digest)
                )
            )
            or type(pin.pinned) is not bool
            or type(pin.retry_until) is not int
            or pin.retry_until < 0
            or (
                pin.message_id is None
                and (pin.digest is not None or pin.pinned or not pin.retry_until)
            )
        ):
            raise ValueError("invalid status pin")
        pins[binding] = pin
    return pins


def _matches(error: TelegramError, *messages: str) -> bool:
    return error.code == 400 and any(m in str(error).lower() for m in messages)


class StatusPins:
    def __init__(
        self,
        api: TelegramApi,
        pins: dict[str, StatusPin],
        save: Callable[[], Awaitable[bool]],
        now: Callable[[], datetime],
    ) -> None:
        self._api, self._pins, self._save, self._now = api, pins, save, now
        self._dirty = False
        self._next_at: datetime | None = None
        self._audit_at: datetime | None = None

    async def _record(self, binding: str, pin: StatusPin | None) -> bool:
        if pin is None:
            self._pins.pop(binding, None)
        else:
            self._pins[binding] = pin
        self._dirty = not await self._save()
        return not self._dirty

    async def refresh(self, chat: int, text: str | None, current: Callable[[], bool]) -> None:
        binding = f"{self._api.bot_id}:{chat}"
        now = self._now().astimezone(UTC)
        if self._dirty:
            self._dirty = not await self._save()
            if self._dirty:
                return
        if self._next_at is not None and now < self._next_at:
            return
        self._next_at = now + timedelta(seconds=INTERVAL_S)
        if not current():
            return
        pin = self._pins.get(binding)
        if pin is not None and now.timestamp() < pin.retry_until:
            return
        try:
            if text is None:
                if pin is not None and pin.message_id is not None:
                    pin = await self._update_text(binding, chat, pin, WITHDRAWN, current)
                    if pin is not None and current():
                        await self._forget(binding, chat, pin)
                return
            await self._publish(binding, chat, text, pin, now, current)
        except TelegramError as error:
            seconds = max(60, error.retry_after or 0) if error.code == 429 else 60
            self._next_at = self._now().astimezone(UTC) + timedelta(seconds=seconds)
            log.warning(
                "Telegram status pin update failed (%s); retry in %d s", error.code, seconds
            )
            if error.code == 429:
                pin = self._pins.get(binding, StatusPin(None, None))
                await self._record(
                    binding, replace(pin, retry_until=math.ceil(self._next_at.timestamp()))
                )

    async def _publish(
        self,
        binding: str,
        chat: int,
        text: str,
        pin: StatusPin | None,
        now: datetime,
        current: Callable[[], bool],
    ) -> None:
        if len(text) > TEXT_LIMIT:
            tail = "\nMore in /status.\nData: NS"
            text = split_text(text, TEXT_LIMIT - len(tail))[0] + tail
        if pin is None or pin.message_id is None:
            await self._private_chat(chat)
            if not current():
                return
            result = await self._api.call(
                "sendMessage",
                {
                    **self._text_params(chat, text),
                    "disable_notification": True,
                    "protect_content": True,
                },
            )
            mid = result.get("message_id") if isinstance(result, dict) else None
            if type(mid) is not int or mid <= 0:
                raise TelegramError(0, None, "invalid status message receipt")
            pin = StatusPin(mid, hashlib.sha256(text.encode()).hexdigest())
            # Save the sent message before pinning it. A pin retry must target this same ID.
            if not await self._record(binding, pin):
                return
        else:
            pin = await self._update_text(binding, chat, pin, text, current)
            if pin is None:
                return
        if pin.pinned:
            if self._audit_at is not None and now < self._audit_at:
                return
            newest = (await self._private_chat(chat)).get("pinned_message")
            if isinstance(newest, dict) and newest.get("message_id") == pin.message_id:
                self._audit_at = now + AUDIT_INTERVAL
                return
            # getChat only exposes the newest pin. Reassert this exact ID, even when another
            # pin is newer. pinChatMessage adds to the list without removing other pins.
        if not current():
            return
        try:
            await self._pin_call("pinChatMessage", chat, pin, disable_notification=True)
        except TelegramError as error:
            if _matches(error, "message to pin not found", "message_id_invalid"):
                await self._forget(binding, chat, pin)
                return
            if not _matches(error, "message is already pinned", "message is not modified"):
                raise
        self._audit_at = now + AUDIT_INTERVAL
        if not pin.pinned:
            await self._record(binding, replace(pin, pinned=True))

    async def _private_chat(self, chat: int) -> dict[str, Any]:
        result = await self._api.call("getChat", {"chat_id": chat})
        if (
            not isinstance(result, dict)
            or result.get("id") != chat
            or result.get("type") != "private"
        ):
            raise TelegramError(0, None, "not the configured private chat")
        return result

    @staticmethod
    def _text_params(chat: int, text: str) -> dict[str, Any]:
        return {"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}

    async def _update_text(
        self, binding: str, chat: int, pin: StatusPin, text: str, current: Callable[[], bool]
    ) -> StatusPin | None:
        digest = hashlib.sha256(text.encode()).hexdigest()
        if pin.digest == digest:
            return pin
        # Telegram may apply an edit before its receipt is lost.
        if pin.digest is not None and not await self._record(binding, replace(pin, digest=None)):
            return None
        if not current():
            return None
        if not await self._edit(chat, pin, text):
            await self._forget(binding, chat, pin)
            return None
        updated = replace(pin, digest=digest)
        return updated if await self._record(binding, updated) else None

    async def _edit(self, chat: int, pin: StatusPin, text: str) -> bool:
        try:
            result = await self._api.call(
                "editMessageText", {**self._text_params(chat, text), "message_id": pin.message_id}
            )
            if not isinstance(result, dict) or result.get("message_id") != pin.message_id:
                raise TelegramError(0, None, "invalid status edit receipt")
        except TelegramError as error:
            if _matches(
                error, "message to edit not found", "message can't be edited", "message_id_invalid"
            ):
                return False
            if not _matches(error, "message is not modified"):
                raise
        return True

    async def _forget(self, binding: str, chat: int, pin: StatusPin) -> None:
        self._audit_at = None
        try:
            await self._pin_call("unpinChatMessage", chat, pin)
        except TelegramError as error:
            if not _matches(
                error, "message is not pinned", "message to unpin not found", "message_id_invalid"
            ):
                raise
        await self._record(binding, None)

    async def _pin_call(self, method: str, chat: int, pin: StatusPin, **params: Any) -> None:
        result = await self._api.call(
            method, {"chat_id": chat, "message_id": pin.message_id, **params}
        )
        if result is not True:
            raise TelegramError(0, None, "invalid status pin receipt")

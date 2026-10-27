"""Telegram Bot API client and the notifier that sends alerts through it (spec 9 step 3)."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from train_commute_companion.model import AlertEvent, Severity

log = logging.getLogger(__name__)

BACKOFF = timedelta(seconds=60)
TEXT_LIMIT = 4096  # sendMessage's limit
DATA_LIMIT = 64  # callback_data bytes


class TelegramError(Exception):
    """`code`: Telegram's `error_code`, 0 for a transport error. The message never holds a URL."""

    def __init__(self, code: int, retry_after: int | None, message: str) -> None:
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


class TelegramApi:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token, self._client = token, client
        self.bot_id = token.split(":", 1)[0]  # safe to log and store
        # httpx logs every request URL, token included, at INFO
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)

    async def call(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        failed = None
        try:
            r = await self._client.post(url, json=params, timeout=timeout)
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:  # the httpx exception carries the URL
            failed = type(e).__name__
        if failed is not None:  # raised outside `except`: no __context__ holds the URL
            raise TelegramError(0, None, failed)
        if not isinstance(body, dict):
            raise TelegramError(0, None, "unexpected body")
        if body.get("ok") is True:
            return body.get("result")
        code, extra = body.get("error_code"), body.get("parameters")
        retry = extra.get("retry_after") if isinstance(extra, dict) else None
        raise TelegramError(
            code if isinstance(code, int) else r.status_code,
            retry if isinstance(retry, int) else None,
            str(body.get("description", "")),
        )


def callback_data(*parts: str) -> str | None:
    """None when it cannot be parsed back or is over Telegram's 64 bytes."""
    data = "|".join(parts)
    if any("|" in p for p in parts) or len(data.encode()) > DATA_LIMIT:
        return None
    return data


def alert_buttons(event: AlertEvent) -> dict[str, Any] | None:
    """`I'm leaving` and `Not travelling` for a journey alert. None for an application warning,
    and for a look-ahead message: it names a future day with no occurrence to act on."""
    d, day = event.direction, event.service_date
    if not event.profile_id or d is None or day is None or event.key.startswith("lookahead|"):
        return None
    row = [
        {"text": label, "callback_data": data}
        for label, tag in (("I'm leaving", "l"), ("Not travelling", "s"))
        if (data := callback_data(tag, event.profile_id, d.value, day.isoformat())) is not None
    ]
    return {"inline_keyboard": [row]} if row else None


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """At the last newline, else the last space, else hard, inside each `limit`."""
    parts: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n", 1, limit + 1), 0) or max(text.rfind(" ", 1, limit + 1), 0)
        if cut:
            parts.append(text[:cut])
            text = text[cut + 1 :]
        else:
            parts.append(text[:limit])
            text = text[limit:]
    return [*parts, text]


class TelegramNotifier:
    """The core's `Notifier`: plain text (no `parse_mode`); silent alerts go without sound."""

    def __init__(self, api: TelegramApi, chat_id: int | None, now: Callable[[], datetime]) -> None:
        self._api, self._chat, self._now = api, chat_id, now
        self._until: datetime | None = None  # global backoff end, UTC
        # per alert (profile, direction, key: the key alone is shared by two profiles' alerts)
        self._key_until: dict[tuple[str, str, str], datetime] = {}  # one bad message's backoff
        # per alert: the payload (text, silent, markup) and how many of its parts were accepted
        self._parts_done: dict[tuple[str, str, str], tuple[tuple[str, bool, str], int]] = {}

    async def send(self, event: AlertEvent) -> bool:
        now = self._now().astimezone(UTC)
        if self._chat is None or (self._until is not None and now < self._until):
            return False
        key = (event.profile_id, event.direction.value if event.direction else "", event.key)
        key_until = self._key_until.get(key)
        if key_until is not None and now < key_until:
            return False
        silent = event.severity is Severity.SILENT
        markup = alert_buttons(event)
        payload = (event.text, silent, json.dumps(markup, sort_keys=True))
        done_for, done = self._parts_done.get(key, (payload, 0))
        parts = split_text(event.text)
        for i in range(done if done_for == payload else 0, len(parts)):
            params: dict[str, Any] = {
                "chat_id": self._chat, "text": parts[i], "disable_notification": silent
            }  # fmt: skip
            if markup is not None and i == len(parts) - 1:
                params["reply_markup"] = markup
            try:
                await self._send_part(params)
            except TelegramError as e:
                self._failed(key, e, now)
                return False
            self._parts_done[key] = (payload, i + 1)
        self._parts_done.pop(key, None)
        self._key_until.pop(key, None)
        return True

    async def _send_part(self, params: dict[str, Any]) -> None:
        try:
            await self._api.call("sendMessage", params)
        except TelegramError as e:
            if e.code != 400 or _chat_not_found(e) or "reply_markup" not in params:
                raise
            # a 400 about this message, for example BUTTON_DATA_INVALID: the text still matters
            log.warning("Telegram refused the buttons (%s); sending without them", e)
            await self._api.call(
                "sendMessage", {k: v for k, v in params.items() if k != "reply_markup"}
            )

    def _failed(self, key: tuple[str, str, str], e: TelegramError, now: datetime) -> None:
        if e.code == 400 and not _chat_not_found(e):  # about this one message only
            self._key_until[key] = now + BACKOFF
            log.warning("Telegram refused a message (400: %s); retried in 60 s", e)
            return
        wait = timedelta(seconds=e.retry_after) if e.code == 429 and e.retry_after else BACKOFF
        self._until = now + wait
        if e.code == 400:
            log.warning("TELEGRAM_CHAT_ID does not name a chat this bot can reach")
        elif e.code == 403:
            log.warning("Telegram 403: the bot is blocked or the chat was never started")
        else:
            log.warning(
                "Telegram send failed (%s: %s); retried in %d s", e.code, e, wait.total_seconds()
            )


def _chat_not_found(e: TelegramError) -> bool:
    return "chat not found" in str(e).lower()

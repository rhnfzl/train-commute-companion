"""Bounded model for overlapping taps on a single Telegram message."""

import asyncio
import json
from itertools import permutations, product

import httpx
from app_rig import CHAT, TOKEN, error, ok
from tcc_app.keyboards import TelegramKeyboards
from tcc_app.telegram import TelegramApi

LEAVE = "l|p1"
SKIP = "s|p1"
BOARDED = "rc|p1|7|1|b"
STOP = "rc|p1|7|1|s"
OTHER_RECOVERY = "rc|p1|8|1|s"
CONFIRM = "ok|proof"
TRAVEL_INFO = "Travel info"

ALL = frozenset({LEAVE, SKIP, BOARDED, STOP, OTHER_RECOVERY, CONFIRM, TRAVEL_INFO})
REMOVED = {
    LEAVE: {LEAVE},
    SKIP: {SKIP},
    BOARDED: {BOARDED, STOP},
    CONFIRM: ALL,
}
MESSAGE = {
    "message_id": 17,
    "reply_markup": {
        "inline_keyboard": [
            [{"text": data, "callback_data": data} for data in (LEAVE, SKIP)],
            [{"text": data, "callback_data": data} for data in (BOARDED, STOP, OTHER_RECOVERY)],
            [{"text": CONFIRM, "callback_data": CONFIRM}],
            [{"text": TRAVEL_INFO, "url": "https://example.com"}],
        ],
    },
}


async def replay(order: tuple[str, ...], failures: tuple[bool, ...]) -> list[frozenset[str]]:
    accepted: list[frozenset[str]] = []
    attempts = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def edit(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        assert request.url.path.endswith("/editMessageReplyMarkup")
        body = json.loads(request.content)
        assert body["chat_id"] == CHAT and body["message_id"] == MESSAGE["message_id"]
        attempt = attempts
        attempts += 1
        if attempt == 0:
            entered.set()
            await release.wait()
        if failures[attempt]:
            return error(400, "edit failed")
        rows = body["reply_markup"]["inline_keyboard"]
        accepted.append(frozenset(button["text"] for row in rows for button in row))
        return ok(True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(edit)) as client:
        keyboard = TelegramKeyboards(TelegramApi(TOKEN, client), lambda: False)

        async def tap(action: str, started: asyncio.Event) -> None:
            started.set()
            await keyboard.retire(CHAT, MESSAGE, action)

        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            tasks.create_task(tap(order[0], asyncio.Event()))
            await entered.wait()
            for action in order[1:]:
                started = asyncio.Event()
                tasks.create_task(tap(action, started))
                await started.wait()
            release.set()
    return accepted


async def test_overlapping_taps_never_restore_retired_controls():
    for order in permutations(REMOVED):
        for failures in product((False, True), repeat=len(order)):
            remaining = ALL
            expected = []
            for action, failed in zip(order, failures, strict=True):
                remaining -= REMOVED[action]
                if not failed:
                    expected.append(remaining)

            assert await replay(order, failures) == expected, (order, failures)

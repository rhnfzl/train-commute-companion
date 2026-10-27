import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from test_runtime import FakeClock, at

CHAT = 1000001
STRANGER = 2000002
TOKEN = "123:TEST"


class FakeTelegram:
    """The Bot API behind a `httpx.MockTransport`, routed on the method at the end of the path."""

    def __init__(self) -> None:
        self.calls: list[str] = []  # every request's method, failed ones included
        self.sent: list[dict[str, Any]] = []  # sendMessage params of answered sends
        self.answered: list[str] = []  # answerCallbackQuery ids
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.fail: Callable[[str], httpx.Response | Exception | None] = lambda method: None
        self._backlog: list[dict[str, Any]] = []  # served until a later offset confirms them
        self._clients: list[httpx.AsyncClient] = []

    def client(self) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        self._clients.append(c)
        return c

    async def close(self) -> None:
        for c in self._clients:
            await c.aclose()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        failure = self.fail(method)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        params = json.loads(request.content) if request.content else {}
        if method == "sendMessage":
            self.sent.append(params)
            return ok({"message_id": len(self.sent)})
        if method == "answerCallbackQuery":
            self.answered.append(params["callback_query_id"])
            return ok(True)
        if method == "getUpdates":
            return ok(await self._get_updates(params))
        return ok(True)

    async def _get_updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        while not self.updates.empty():
            self._backlog.append(self.updates.get_nowait())
        offset = params.get("offset")
        if offset == -1:  # as Telegram: only the last update, the rest are forgotten
            self._backlog = self._backlog[-1:]
        elif offset:
            self._backlog = [u for u in self._backlog if u["update_id"] >= offset]
        if not self._backlog:
            try:
                u = await asyncio.wait_for(self.updates.get(), params.get("timeout", 0))
            except TimeoutError:
                return []
            self._backlog.append(u)
        return list(self._backlog)


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def error(code: int, description: str = "", retry_after: int | None = None) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(code, json=body)


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    fake = FakeTelegram()
    yield fake
    await fake.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(at("07:50"))

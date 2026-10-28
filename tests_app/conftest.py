import asyncio
import dataclasses
import json
import re
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from tcc_app import bot as bot_mod
from tcc_app.bot import ShellState, save_state
from tcc_app.follow import Fix
from tcc_app.main import App, Settings, build
from test_runtime import FakeClock, at

from train_commute_companion.model import Severity
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import PlannedWorks, Provider, ProviderAnswer, Query
from train_commute_companion.replay import Replay, apply_event, load_replay

CHAT = 1000001
STRANGER = 2000002
TOKEN = "123:TEST"


class FakeTelegram:
    """The Bot API behind a `httpx.MockTransport`, routed on the method at the end of the path."""

    def __init__(self) -> None:
        self.calls: list[str] = []  # every request's method, failed ones included
        self.sent: list[dict[str, Any]] = []  # sendMessage params of answered sends
        self.answered: list[str] = []  # answerCallbackQuery ids
        self.polls: list[dict[str, Any]] = []  # getUpdates params, failed ones included
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.fail: Callable[[str], httpx.Response | Exception | None] = lambda method: None
        # every request to another host (a webhook receiver) goes here
        self.other: Callable[[httpx.Request], httpx.Response] | None = None
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
        if request.url.host != "api.telegram.org":
            assert self.other is not None, f"unexpected request to {request.url.host}"
            return self.other(request)
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        params = json.loads(request.content) if request.content else {}
        if method == "getUpdates":
            self.polls.append(params)
        failure = self.fail(method)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
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


class TickClock(FakeClock):
    """`sleep` advances the time, then yields for real, so `run_forever`'s loops take turns."""

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        await asyncio.sleep(0.01)


class StationsProvider:
    """`inner` with a station list: `build` requires one before a configuration applies."""

    def __init__(
        self,
        inner: Provider,
        codes: frozenset[str],
        positions: Mapping[str, tuple[float, float]] = {},
    ) -> None:
        self.inner, self.codes, self.positions = inner, codes, dict(positions)

    async def trips(self, q: Query) -> ProviderAnswer:
        return await self.inner.trips(q)

    async def planned_works(self) -> PlannedWorks:
        return await self.inner.planned_works()

    async def stations(self) -> frozenset[str]:
        return self.codes


def spied(app: Any) -> list[tuple[str | None, str]]:
    """What reached `rt.location`, as `(zone, kind)`; installs the spy on first use."""
    if not hasattr(app, "calls"):
        app.calls = calls = []
        orig = app.rt.location

        async def spy(profile_id: str, event: Any, *rest: Any, **kw: Any) -> None:
            calls.append((event.zone, event.kind))
            await orig(profile_id, event, *rest, **kw)

        app.rt.location = spy
    return list(app.calls)


async def feed(app: Any, lat: float, lon: float, when: str | datetime, acc: float | None = None,
               regions: tuple[str, ...] = (), now: str | datetime | None = None) -> None:  # fmt: skip
    """One fix from the profile's source, handled at its own time (or at `now`)."""
    t = at(when) if isinstance(when, str) else when
    app.clock.t = t if now is None else at(now) if isinstance(now, str) else now
    spied(app)
    source = app.rt.view().profiles[0].location.source
    app.follower.feed(source, Fix(lat, lon, t, acc, frozenset(regions)))
    await app.follower.drain()


def build_from_replay(
    tmp_path: Path, tg: FakeTelegram, path: Path, **settings: Any
) -> tuple[App, Replay]:
    """The app on a replay's clock and scripted NS, with the station list a real provider has;
    `settings` replace fields of its `Settings`."""
    rp = load_replay(path)
    cfg, errors = parse_profiles(rp.profile_path.read_text(encoding="utf-8"), None)
    assert not errors
    codes = frozenset(
        c
        for p in cfg
        for c in (p.home_station, p.work_station, *p.outbound.transfers, *p.return_.transfers)
    )
    s = dataclasses.replace(Settings("k", TOKEN, CHAT, None, rp.profile_path, tmp_path), **settings)
    app = build(
        s, clock=rp.clock, provider=StationsProvider(rp.provider, codes), client=tg.client()
    )
    return app, rp


LINE = re.compile(r"\d\d:\d\d (LOUD|SILENT) \w+: (.*)")


def expected_lines(path: Path) -> list[tuple[Severity, str]]:
    """A replay `.out` file's alerts; the closing `Data: NS` line is skipped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [(Severity[m[1]], m[2]) for x in lines if (m := LINE.fullmatch(x))]


async def drive_replay(app: App, rp: Replay) -> None:
    """`replay_lines`' loop on `app.rt`: events, a poll on even minutes, the watchdog, 30 s steps;
    each step lets a started webhook pass finish, as a real sleep would."""
    clock, pending = rp.clock, list(rp.timeline)
    await app.rt.start()
    while clock.now().astimezone(UTC) < rp.end.astimezone(UTC):
        while pending and pending[0][0].astimezone(UTC) <= clock.now().astimezone(UTC):
            await apply_event(app.rt, pending.pop(0)[1], clock.now())
        now = clock.now()
        if now.minute % 2 == 0 and now.second == 0:
            await app.rt.poll_once()
        await app.rt.watchdog_once()
        await app.rt.webhooks_idle()
        await clock.sleep(30)


def gated_saves(monkeypatch) -> tuple[threading.Event, threading.Event, list[int], list[int]]:
    """The first save blocks on the gate; written offsets in order, and the peak of overlap."""
    entered, gate, guard = threading.Event(), threading.Event(), threading.Lock()
    active, peak, written = [0], [0], []

    def spy(path: Path, s: ShellState) -> None:
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        if not entered.is_set():
            entered.set()
            gate.wait(2)
        save_state(path, s)
        with guard:
            active[0] -= 1
            written.append(s.offset)

    monkeypatch.setattr(bot_mod, "save_state", spy)
    return entered, gate, written, peak

"""The Telegram bot: commands, buttons, the delivery test and the chat allow-list (spec 9 step 3,
7.12 rules 15 and 16). Thin: every train decision is the core's."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, TypeVar

from train_commute_companion.lifecycle import Leaving, NotTravelling
from train_commute_companion.model import Direction
from train_commute_companion.runtime import Runtime

from .commands import _pretty, command_word, parse_target, render_status, status_buttons
from .telegram import TelegramApi, TelegramError, split_text

log = logging.getLogger(__name__)

T = TypeVar("T")

COMMAND_TIMEOUT_S = 120.0
RETRY_S = 5.0  # after a poll error
CONFLICT_RETRY_S = 30.0  # after a 409: a webhook is set or a second instance polls
POLL_S = 30  # long poll; the HTTP timeout is 10 s longer
MAX_AGE = timedelta(minutes=10)  # an older command is a backlog that outlived its cursor
ALLOWED = ["message", "edited_message", "callback_query"]

LOUD_TEST = "Train Commute Companion: test alert, loud. You should hear this one."
SILENT_TEST = "Train Commute Companion: test alert, silent. This one should arrive without sound."
USAGE_CHOOSE = "Usage: /choose [profile] [outbound|return] HH:MM [train number] [YYYY-MM-DD]"
HELP = (
    "/status: the journeys being followed now\n"
    "/choose [profile] [outbound|return] HH:MM [train number] [YYYY-MM-DD]: lock that train\n"
    "/leaving [profile] [outbound|return]: I'm leaving now\n"
    "/skip [profile] [outbound|return|day] [YYYY-MM-DD]: not travelling, alerts for it stop\n"
    "/test: send the loud and the silent test alert\n"
    "/help: this list\n"
    "Data: NS"
)


@dataclass
class ShellState:
    offset: int = 0
    bot_id: str | None = None  # the bot whose update ids `offset` counts
    confirmed_for: str | None = None  # "<bot id>:<chat id>" (rule 16 proves one target)


def load_state(path: Path) -> ShellState | None:
    try:
        raw = json.loads(path.read_text())
        s = ShellState(**raw)
        if not isinstance(s.offset, int) or not all(
            v is None or isinstance(v, str) for v in (s.bot_id, s.confirmed_for)
        ):
            raise ValueError("wrong types")
    except FileNotFoundError:
        log.info("no Telegram state file at %s", path)
        return None
    except (OSError, ValueError, TypeError) as e:
        log.warning("Telegram state file %s is unreadable (%s)", path, type(e).__name__)
        return None
    return s


def save_state(path: Path, s: ShellState) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(asdict(s)))
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


class _TimedOut(Exception):
    pass


class Bot:
    def __init__(
        self, api: TelegramApi, rt: Runtime, chat_id: int | None, state_path: Path
    ) -> None:
        self._api, self._rt, self._chat, self._path = api, rt, chat_id, state_path
        loaded = load_state(state_path)
        self._trusted = loaded is not None and loaded.bot_id == api.bot_id
        if loaded is not None and self._trusted:
            self.state = loaded
        else:  # another bot's or no cursor: only the confirmation (it names its bot) is kept
            self.state = ShellState(confirmed_for=loaded.confirmed_for if loaded else None)
        self._nonce: str | None = None
        self._save_failing = False
        self._tasks: set[asyncio.Task[None]] = set()  # handlers, cancelled by close()
        self._core: set[asyncio.Task[Any]] = set()  # runtime calls, never cancelled here

    @property
    def confirmed(self) -> bool:
        return self.state.confirmed_for == f"{self._api.bot_id}:{self._chat}"

    # --- polling ---

    async def run(self) -> None:
        while True:
            try:
                if not self._trusted:
                    await self._discard_backlog()
                ups = await self._api.call(
                    "getUpdates",
                    {"offset": self.state.offset, "timeout": POLL_S, "allowed_updates": ALLOWED},
                    timeout=POLL_S + 10,
                )
                await self._take(_updates(ups))
            except TelegramError as e:
                log.warning("Telegram getUpdates failed (%s: %s)", e.code, e)
                await asyncio.sleep(CONFLICT_RETRY_S if e.code == 409 else RETRY_S)

    async def _discard_backlog(self) -> None:
        # Telegram forgets every update before the one offset=-1 returns
        ups = _updates(await self._api.call("getUpdates", {"offset": -1, "timeout": 0}))
        self.state.offset = ups[-1]["update_id"] + 1 if ups else 0
        self.state.bot_id = self._api.bot_id
        self._trusted = True
        self._save()
        log.warning("no saved Telegram offset for this bot; queued updates discarded")

    async def _take(self, ups: list[dict[str, Any]]) -> None:
        """Durable first, then dispatched: no handled update can come back after a restart."""
        while ups:
            old = self.state.offset
            self.state.offset = ups[-1]["update_id"] + 1
            if self._save():
                self._spawn(ups)
                return
            # the stale file may stay readable: confirm the batch to Telegram before handling it
            try:
                nxt = await self._api.call(
                    "getUpdates", {"offset": self.state.offset, "timeout": 0}, timeout=POLL_S + 10
                )
            except TelegramError:
                self.state.offset = old  # not dispatched: Telegram serves it again
                raise
            self._spawn(ups)
            ups = _updates(nxt)

    def _save(self) -> bool:
        try:
            save_state(self._path, self.state)
        except OSError as e:
            if not self._save_failing:
                log.warning("cannot save Telegram state (%s); retrying", type(e).__name__)
            self._save_failing = True
            return False
        if self._save_failing:
            log.warning("Telegram state saved again")
        self._save_failing = False
        return True

    def _spawn(self, ups: list[dict[str, Any]]) -> None:
        for u in ups:
            task = asyncio.create_task(self.handle(u))
            self._tasks.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Telegram update failed", exc_info=task.exception())

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- one update ---

    async def handle(self, update: Mapping[str, Any]) -> None:
        msg, edited, cb = (_obj(update.get(k)) for k in ALLOWED)
        chat = _obj((msg or edited or _obj(cb.get("message"))).get("chat")).get("id")
        if self._chat is None or chat != self._chat:
            log.warning(
                "ignored an update from chat %s; set TELEGRAM_CHAT_ID to this id to use this chat",
                chat,
            )
            return
        try:
            if cb:
                await self._button(cb)
            elif msg and isinstance(msg.get("text"), str) and "location" not in msg:
                await self._message(msg)
            # an edited message is read only for its location, and locations are Task 13's
        except _TimedOut:
            await self._reply("NS did not answer in time; try again.")

    async def _message(self, msg: dict[str, Any]) -> None:
        sent = msg.get("date")
        if not isinstance(sent, int):
            return
        age = self._rt.now().astimezone(UTC) - datetime.fromtimestamp(sent, UTC)
        if age > MAX_AGE:
            log.info("ignored a Telegram command sent %d min ago", age.total_seconds() // 60)
            return
        text: str = msg["text"]
        word, args = command_word(text), text.split()[1:]
        if word in ("/start", "/help"):
            await self._reply(HELP)
            if not self.confirmed:
                await self._test_pair()
        elif word == "/test":
            await self._test_pair()
        elif word == "/status":
            view = self._rt.view()
            await self._reply(render_status(view, self.confirmed), status_buttons(view))
        elif word in ("/choose", "/leaving", "/skip"):
            t = parse_target(self._rt.view(), args)
            if isinstance(t, str):
                await self._reply(t)
                return
            name = _pretty(t.profile_name)
            if word == "/choose":
                if t.departure is None:
                    await self._reply(USAGE_CHOOSE)
                    return
                await self._choose(
                    t.profile_id, name, t.departure, t.train, t.service_date, t.direction
                )
            elif word == "/leaving":
                await self._leaving(t.profile_id, name, t.service_date, t.direction)
            else:
                whole = t.whole_day or t.direction is None
                await self._skip(t.profile_id, name, t.service_date, t.direction, whole)

    async def _button(self, cb: dict[str, Any]) -> None:
        try:
            await self._api.call("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        except TelegramError as e:
            log.warning("Telegram answerCallbackQuery failed (%s)", e.code)
        data = cb.get("data")
        parts = data.split("|") if isinstance(data, str) else []
        if parts[:1] == ["ok"] and len(parts) == 2:
            if self._nonce is not None and secrets.compare_digest(
                parts[1].encode(), self._nonce.encode()
            ):
                self.state.confirmed_for = f"{self._api.bot_id}:{self._chat}"
                self._save()
                self._nonce = None
                await self._reply("Delivery confirmed. Setup is complete.")
            else:
                await self._reply("That test is old; send /test again.")
            return
        try:
            tag, pid, d, day, *rest = parts
            direction, service_date = Direction(d), date.fromisoformat(day)
            at = time.fromisoformat(rest[0]) if tag == "c" and rest else None
        except ValueError:  # unparseable: answered, ignored
            return
        names = {x.profile_id: x.profile_name for x in self._rt.view().directions}
        name = _pretty(names.get(pid, pid))
        if tag == "l" and not rest:
            await self._leaving(pid, name, service_date, direction)
        elif tag == "s" and not rest:
            await self._skip(pid, name, service_date, direction, False)
        elif at is not None and len(rest) <= 2:
            await self._choose(pid, name, at, rest[1] if len(rest) == 2 else None,
                               service_date, direction)  # fmt: skip

    # --- actions ---

    async def _choose(
        self, pid: str, name: str, at: time, train: str | None,
        day: date | None, direction: Direction | None,
    ) -> None:  # fmt: skip
        reason = await self._call(self._rt.choose_at(pid, at, train, day, direction))
        await self._reply(f"Locked the {at:%H:%M} for {name}." if reason is None else reason)

    async def _leaving(
        self, pid: str, name: str, day: date | None, direction: Direction | None
    ) -> None:
        done = await self._call(self._rt.act(pid, Leaving(day, self._rt.now()), direction))
        await self._reply("Marked as leaving now." if done else _nothing(name))

    async def _skip(
        self, pid: str, name: str, day: date | None, direction: Direction | None, whole: bool
    ) -> None:
        done = await self._call(self._rt.act(pid, NotTravelling(day, whole), direction))
        if not done:
            await self._reply(_nothing(name))
        elif whole:
            await self._reply("Not travelling today: alerts for both directions stop.")
        else:
            await self._reply("Not travelling: alerts for that journey stop.")

    async def _call(self, aw: Awaitable[T]) -> T:
        """The runtime call is its own task: a timeout or a cancelled handler leaves it to finish
        (a cancelled delivery pass after a send would repeat that alert)."""
        task = asyncio.ensure_future(aw)
        self._core.add(task)
        task.add_done_callback(self._core.discard)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_S):
                return await asyncio.shield(task)
        except TimeoutError:
            raise _TimedOut from None

    # --- sending ---

    async def _test_pair(self) -> None:
        self._nonce = None
        nonce = secrets.token_hex(4)
        button = {"inline_keyboard": [[{"text": "I got both", "callback_data": f"ok|{nonce}"}]]}
        try:
            await self._send(LOUD_TEST, disable_notification=False)
            await self._send(SILENT_TEST, disable_notification=True, reply_markup=button)
        except TelegramError as e:
            log.warning("Telegram test alert failed (%s: %s); send /test again", e.code, e)
            return
        self._nonce = nonce

    async def _reply(self, text: str, markup: dict[str, Any] | None = None) -> None:
        parts = split_text(text)
        try:
            for i, part in enumerate(parts):
                extra = {"reply_markup": markup} if markup and i == len(parts) - 1 else {}
                await self._send(part, **extra)
        except TelegramError as e:
            log.warning("Telegram reply failed (%s: %s)", e.code, e)

    async def _send(self, text: str, **params: Any) -> None:
        await self._api.call("sendMessage", {"chat_id": self._chat, "text": text, **params})


def _nothing(name: str) -> str:
    return f"Nothing to act on: no journey of {name} is being followed now."


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _updates(v: Any) -> list[dict[str, Any]]:
    return (
        [u for u in v if isinstance(u, dict) and isinstance(u.get("update_id"), int)]
        if isinstance(v, list)
        else []
    )

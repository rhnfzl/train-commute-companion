"""The Telegram bot: commands, buttons, the delivery test and the chat allow-list (spec 9 step 3,
7.12 rules 15 and 16). Thin: every train decision is the core's."""

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, TypeVar

import segno

from train_commute_companion.catalogue import message as localized
from train_commute_companion.compensation import ArrivalError, arrival_text
from train_commute_companion.compensation import confirm as check_arrival
from train_commute_companion.history import STATS_PERIODS, stats_text
from train_commute_companion.location import Fix, number
from train_commute_companion.messages import RefusalKind, Refused
from train_commute_companion.model import Direction, display_name
from train_commute_companion.recovery_state import RecoveryActionKind
from train_commute_companion.runtime import Runtime
from train_commute_companion.tasks import DetachedCall
from train_commute_companion.webhook import WebhookTestResult

from .bot_state import (
    ArrivalDraft,
    CursorPhase,
    DeliveryProof,
    ProofPhase,
    ReviewPhase,
    SavePhase,
    SetupClaim,
    UpdateCursor,
)
from .commands import (
    command_word,
    parse_arrival,
    parse_target,
    profile_language,
    recovery_offer_buttons,
    recovery_origin_prompt,
    recovery_origins,
    recovery_progress_buttons,
    render_recovery_offer,
    render_selected_recovery,
    render_status,
    render_webhook_tests,
    shared_language,
    stations_text,
    status_buttons,
    view_for_profiles,
)
from .follow import FUTURE_LIMIT, Follower, stamp
from .setup_wizard import SetupWizard
from .status_pin import INTERVAL_S, StatusPin, StatusPins, load_pins
from .telegram import TelegramApi, TelegramError, split_text, telegram_recipient
from .travel_actions import POLICY_LABELS, TravelActions
from .travel_chat import TravelChat, TravelReply, buttons, plan_receipt
from .travel_language import TravelLanguage

log = logging.getLogger(__name__)

T = TypeVar("T")

COMMAND_TIMEOUT_S = 120.0
RETRY_S = 5.0  # after a poll error
CONFLICT_RETRY_S = 30.0  # after a 409: a webhook is set or a second instance polls
POLL_S = 30  # long poll; the HTTP timeout is 10 s longer
MAX_AGE = timedelta(minutes=10)  # an older command is a backlog that outlived its cursor
ALLOWED = ["message", "edited_message", "callback_query"]

LOUD_TEST = localized("command.test_loud", "en")
SILENT_TEST = localized("command.test_silent", "en")
USAGE_CHOOSE = localized("command.choose_usage", "en")
HELP = localized("command.help", "en")
TIMED_OUT = "NS did not answer in time; try again."
FAILED = "That failed; see the app log."
INVALID = "That button is no longer valid."
# the reply names the tapped button; these stand in when its message or keyboard is missing
BUTTON_LABELS = {
    "av": "Confirm arrival",
    "q": "Answer",
    "ok": "I got both",
    **dict.fromkeys(("rp", "ra", "rf", "rc"), "Recovery"),
    "l": "I'm leaving",
    "s": "Not travelling",
    "c": "Take train",
    "tp": "Travel plan",
    "ta": "Travel approval",
    "ag": "Agent access",
    "tm": "Travel preference",
}
# exclusive one-time choices: a used one takes its whole keyboard; any other used button goes alone
WHOLE_KEYBOARD = frozenset({"ok", "av", "ap", "q", "ra", "tp", "ta", "ag", "tm", "setup"})


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a button led to. `done`: the action took effect, so its button (or, for a one-time
    choice, its whole keyboard) goes."""

    text: str
    markup: dict[str, Any] | None = None
    done: bool = False


@dataclass(slots=True)
class ShellState:
    offset: int = 0
    bot_id: str | None = None  # the bot whose update ids `offset` counts
    confirmed_for: str | None = None  # "<bot id>:<chat id>" (rule 16 proves one target)
    # the "I got both" nonce that proved it, saved with it; None in an older file: unknown
    accepted_nonce: str | None = None
    claimed_for: str | None = None  # "<bot id>:<private chat id>"; never the setup code
    status_pins: dict[str, StatusPin] = field(default_factory=dict)


def load_state(path: Path) -> ShellState | None:
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("not a regular state file")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("invalid Telegram state")
        pins = load_pins(raw.pop("status_pins", {}))
        if pins and mode & 0o077:
            raise ValueError("status pin storage must be private")
        s = ShellState(**raw)
        s.status_pins = pins
        if (
            isinstance(s.offset, bool)
            or not isinstance(s.offset, int)
            or s.offset < 0
            or not all(
                v is None or isinstance(v, str)
                for v in (s.bot_id, s.confirmed_for, s.accepted_nonce)
            )
            or (
                s.claimed_for is not None
                and (
                    not isinstance(s.claimed_for, str)
                    or re.fullmatch(r"[0-9]{1,20}:[1-9][0-9]{0,19}", s.claimed_for) is None
                )
            )
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
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as f:
            tmp = Path(f.name)
            f.write(json.dumps(asdict(s)))
            f.flush()
            os.fsync(f.fileno())  # a power loss must not leave an empty file behind the rename
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        raise


class _TimedOut(Exception):
    pass


def _log_foreign_chat(chat: object) -> None:
    # startup refuses a group id, so only a private chat may be pointed at it
    private = isinstance(chat, int) and chat > 0
    hint = (
        "set TELEGRAM_CHAT_ID to this id to use this chat"
        if private
        else "not a private chat, so it cannot be TELEGRAM_CHAT_ID"
    )
    log.warning("ignored an update from chat %s; %s", chat, hint)


class Bot:
    def __init__(
        self,
        api: TelegramApi,
        rt: Runtime,
        chat_id: int | None,
        state_path: Path,
        follower: Follower | None = None,
        webhook_test: Callable[[], Awaitable[Sequence[WebhookTestResult]]] | None = None,
        bind_chat: Callable[[int], None] | None = None,
        *,
        travel_actions: TravelActions | None = None,
        travel_language: TravelLanguage | None = None,
        setup_path: Path | None = None,
    ) -> None:
        self._api, self._rt, self._chat, self._path = api, rt, chat_id, state_path
        self._follower, self._webhook_test = follower, webhook_test
        self._wizard = SetupWizard(rt, setup_path) if setup_path is not None else None
        loaded = load_state(state_path)
        # An unreadable binding must never turn back into an open setup link.
        self._state_safe = loaded is not None or not os.path.lexists(state_path)
        if chat_id is not None:  # explicit routing retains the existing state-repair behavior
            self._state_safe = True
        self._bind_chat = bind_chat
        trusted = loaded is not None and loaded.bot_id == api.bot_id
        self._cursor = UpdateCursor(CursorPhase.READY if trusted else CursorPhase.UNTRUSTED)
        if loaded is not None and trusted:
            self.state = loaded
        elif loaded is not None:  # binding records carry their own bot IDs
            self.state = ShellState(
                confirmed_for=loaded.confirmed_for,
                accepted_nonce=loaded.accepted_nonce,
                claimed_for=loaded.claimed_for,
                status_pins=loaded.status_pins,
            )
        else:
            self.state = ShellState()
        binding = self.state.claimed_for
        if self._chat is None and binding is not None:
            bot_id, saved_chat = binding.split(":")
            if bot_id == api.bot_id:
                self._chat = int(saved_chat)
        if self._chat is not None and bind_chat is not None:
            bind_chat(self._chat)
        self._claim: SetupClaim | None = None
        self._proof: DeliveryProof | None = None
        self._arrival_drafts: dict[str, ArrivalDraft] = {}
        self._stats_clear: tuple[str, datetime, frozenset[str]] | None = None
        self.travel_actions = travel_actions or TravelActions(
            rt, {}, state_path.with_name("travel-agent-access.json")
        )
        self._travel_chat = TravelChat(
            rt, self.travel_actions.preferences, travel_language, self.travel_actions
        )
        self._travel_lock = asyncio.Lock()
        self._save_phase = SavePhase.READY
        self._save_lock = asyncio.Lock()  # one save at a time: no shared .tmp, the newest wins
        self._keyboard_lock = asyncio.Lock()
        self._retired: dict[int, set[str] | None] = {}
        # saves: drained by close(), never cancelled (serve's cleanup leaves them out too)
        self.writes: set[asyncio.Task[bool]] = set()
        self._tasks: set[asyncio.Task[None]] = set()  # handlers, cancelled by close()
        self._core: set[asyncio.Task[Any]] = set()  # runtime calls, never cancelled here
        self._status_pins = StatusPins(api, self.state.status_pins, self._save, rt.now)

    @property
    def confirmed(self) -> bool:
        return self._chat is not None and self.state.confirmed_for == (
            f"{self._api.bot_id}:{self._chat}"
        )

    async def _run_status(self) -> None:
        while not self._cursor.closed:
            await self.refresh_status_pin()
            await asyncio.sleep(INTERVAL_S)

    async def refresh_status_pin(self) -> None:
        chat, proof = self._chat, self.state.confirmed_for
        if chat is None or chat <= 0 or not self.confirmed or not self._rt.started:
            return

        def eligible() -> set[str]:
            config = self._rt.config
            return (
                {p.id for p in config.profiles if p.alerts.telegram_status_pin} if config else set()
            )

        enabled = eligible()
        text = (
            render_status(view_for_profiles(self._rt.view(), enabled), True, for_pin=True)
            if enabled
            else None
        )
        await self._status_pins.refresh(
            chat,
            text,
            lambda: (
                self._chat == chat and self.state.confirmed_for == proof and eligible() == enabled
            ),
        )

    @property
    def arrival_recipient(self) -> str | None:
        if not self.confirmed or self._chat is None or self._chat <= 0:
            return None
        return telegram_recipient(self._api.bot_id, self._chat)

    async def _setup_claim(self) -> None:
        if self._chat is not None or self._claim is not None or not self._state_safe:
            return
        identity = _obj(await self._api.call("getMe", {}))
        username = identity.get("username")
        if (
            identity.get("is_bot") is not True
            or str(identity.get("id")) != self._api.bot_id
            or not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username)
        ):
            raise TelegramError(0, None, "invalid bot identity")
        claim = SetupClaim(secrets.token_urlsafe(32), self._rt.now())
        link = f"https://t.me/{username}?start={claim.code}"
        qr = StringIO()
        segno.make(link).terminal(out=qr, compact=True)
        self._claim = claim
        log.info(
            "Private Telegram setup: open %s and press Start, or scan this QR. "
            "Link expires in 15 minutes; restart for a new link. Keep setup logs private.\n%s",
            link,
            qr.getvalue(),
        )

    async def _try_claim(self, msg: dict[str, Any]) -> bool:
        chat = _obj(msg.get("chat"))
        sent = stamp(msg.get("date"))
        args = msg["text"].split()[1:]
        claim = self._claim
        if (
            self._chat is not None
            or claim is None
            or len(args) != 1
            or chat.get("type") != "private"
            or type(chat.get("id")) is not int
            or chat["id"] <= 0
            or sent is None
        ):
            return False
        now = self._rt.now().astimezone(UTC)
        if not -FUTURE_LIMIT <= now - sent.astimezone(UTC) <= MAX_AGE:
            return False
        if not claim.accepts(args[0], now):
            return False
        return await self._save(claimed_chat=chat["id"])

    # --- polling ---

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            status = tasks.create_task(self._run_status(), name="telegram-status")
            try:
                await self._poll()
            finally:
                status.cancel()

    async def _poll(self) -> None:
        while not self._cursor.closed:
            try:
                if self._cursor.phase is CursorPhase.UNTRUSTED:
                    await self._discard_backlog()
                if self._cursor.phase is CursorPhase.CLOSED:
                    return
                await self._setup_claim()
                ups = await self._api.call(
                    "getUpdates",
                    {"offset": self.state.offset, "timeout": POLL_S, "allowed_updates": ALLOWED},
                    timeout=POLL_S + 10,
                )
                await self._commit_and_dispatch(_updates(ups))
            except TelegramError as e:
                log.warning("Telegram polling or setup failed (%s); retrying", e.code)
                await asyncio.sleep(CONFLICT_RETRY_S if e.code == 409 else RETRY_S)

    async def _discard_backlog(self) -> None:
        # Telegram forgets every update before the one offset=-1 returns
        ups = _updates(await self._api.call("getUpdates", {"offset": -1, "timeout": 0}))
        if self._cursor.phase is CursorPhase.CLOSED:
            return
        self.state.offset = ups[-1]["update_id"] + 1 if ups else 0
        self.state.bot_id = self._api.bot_id
        self._cursor.phase = CursorPhase.READY
        await self._save()
        log.warning("no saved Telegram offset for this bot; queued updates discarded")

    async def _commit_and_dispatch(self, ups: list[dict[str, Any]]) -> None:
        """Durable first, then dispatched: no handled update can come back after a restart."""
        ups = [u for u in _updates(ups) if u["update_id"] >= self.state.offset]
        while ups and not self._cursor.closed:
            old = self.state.offset
            self._cursor.begin()
            self.state.offset = ups[-1]["update_id"] + 1
            if await self._save():
                if self._cursor.release():
                    self._spawn(ups)
                return
            # the stale file may stay readable: confirm the batch to Telegram before handling it
            try:
                if self._cursor.phase is CursorPhase.CLOSED:
                    return
                self._cursor.awaiting_ack()
                nxt = await self._api.call(
                    "getUpdates", {"offset": self.state.offset, "timeout": 0}, timeout=POLL_S + 10
                )
            except TelegramError:
                self.state.offset = old  # not dispatched: Telegram serves it again
                self._cursor.release()
                raise
            if not self._cursor.release():
                return
            self._spawn(ups)
            ups = [u for u in _updates(nxt) if u["update_id"] >= self.state.offset]

    async def _save(
        self,
        confirmed_for: str | None = None,
        nonce: str | None = None,
        *,
        claimed_chat: int | None = None,
    ) -> bool:
        """`confirmed_for` and the `nonce` that proved it are written together or not at all."""
        # a cancelled caller leaves the write running and the lock held until its worker ends
        task = asyncio.ensure_future(self._write(confirmed_for, nonce, claimed_chat=claimed_chat))
        self.writes.add(task)
        task.add_done_callback(self.writes.discard)
        return await asyncio.shield(task)

    async def _write(
        self,
        confirmed_for: str | None = None,
        nonce: str | None = None,
        *,
        claimed_chat: int | None = None,
    ) -> bool:
        async with self._save_lock:
            if not self._state_safe or (
                claimed_chat is not None
                and (
                    self._chat is not None
                    or self._cursor.closed
                    or self._claim is None
                    or not self._claim.accepts(self._claim.code, self._rt.now())
                )
            ):
                return False
            snap = replace(self.state, status_pins=self.state.status_pins.copy())
            if confirmed_for is not None:
                snap.confirmed_for, snap.accepted_nonce = confirmed_for, nonce
            if claimed_chat is not None:
                snap.claimed_for = f"{self._api.bot_id}:{claimed_chat}"
                snap.bot_id = self._api.bot_id
            try:
                await asyncio.to_thread(save_state, self._path, snap)  # off the loop (rule 12)
            except OSError as e:
                if self._save_phase is SavePhase.READY:
                    log.warning("cannot save Telegram state (%s); retrying", type(e).__name__)
                self._save_phase = SavePhase.FAILED
                return False
            if confirmed_for is not None:
                self.state.confirmed_for, self.state.accepted_nonce = confirmed_for, nonce
            if claimed_chat is not None:
                self.state.claimed_for, self.state.bot_id = snap.claimed_for, snap.bot_id
                self._chat, self._claim = claimed_chat, None
                if self._bind_chat is not None:
                    self._bind_chat(claimed_chat)
            if self._save_phase is SavePhase.FAILED:
                log.warning("Telegram state saved again")
            self._save_phase = SavePhase.READY
            return True

    def _spawn(self, ups: list[dict[str, Any]]) -> None:
        if self._cursor.phase is CursorPhase.CLOSED:
            return
        for u in ups:
            task = asyncio.create_task(self.handle(u))
            self._tasks.add(task)
            task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Telegram update failed", exc_info=task.exception())

    async def close(self) -> None:
        self._cursor.phase = CursorPhase.CLOSED
        self._claim = None
        self._proof = None
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*self.writes, return_exceptions=True)

    # --- one update ---

    async def handle(self, update: Mapping[str, Any]) -> None:
        if self._cursor.phase is CursorPhase.CLOSED:
            return
        msg, edited, cb = (_obj(update.get(k)) for k in ALLOWED)
        text = msg.get("text")
        if (
            isinstance(text, str)
            and command_word(text) == "/start"
            and text.split()[1:]
            and not await self._try_claim(msg)
        ):
            return
        chat = _obj((msg or edited or _obj(cb.get("message"))).get("chat")).get("id")
        if self._chat is None or chat != self._chat:
            _log_foreign_chat(chat)
            return
        if not cb and "location" in (msg or edited):
            self._location(msg or edited)  # no command: a failure is logged, never answered
            return
        try:
            if cb:
                action = cb.get("data")
                if (
                    isinstance(action, str)
                    and action.startswith("ap|")
                    and (
                        _obj(cb.get("from")).get("id") != chat
                        or _obj(_obj(cb.get("message")).get("chat")).get("type") != "private"
                    )
                ):
                    return
                await self._button(cb)
            elif msg and isinstance(msg.get("text"), str):
                await self._message(msg)
            # an edited message is read only for its location (live location moves edit it)
        except _TimedOut:
            await self._reply(TIMED_OUT)
        except Exception:
            # the exception text stays out of the chat; without a reply the user cannot tell
            # whether a skip or a lock took effect
            log.exception("Telegram command failed")
            await self._reply(FAILED)

    def _location(self, msg: dict[str, Any]) -> None:
        loc = _obj(msg.get("location"))
        lat, lon = number(loc.get("latitude")), number(loc.get("longitude"))
        at = stamp(msg.get("edit_date") or msg.get("date"))
        acc = number(loc.get("horizontal_accuracy"))
        if self._follower is None or lat is None or lon is None or at is None:
            return
        if "horizontal_accuracy" in loc and acc is None:
            return  # a malformed accuracy is not "accuracy unknown": the fix is not trusted
        self._follower.feed("telegram", Fix(lat, lon, at, acc))

    async def _message(self, msg: dict[str, Any]) -> None:
        sent = stamp(msg.get("date"))
        if sent is None:
            return
        age = self._rt.now().astimezone(UTC) - sent.astimezone(UTC)
        if age < -FUTURE_LIMIT:
            log.info("ignored a Telegram command with a future timestamp")
            return
        if age > MAX_AGE:
            log.info("ignored a Telegram command sent %d min ago", age.total_seconds() // 60)
            return
        text: str = msg["text"]
        word, args = command_word(text), text.split()[1:]
        if word in ("/start", "/help"):
            await self._reply(localized("command.help", shared_language(self._rt.view())))
            if not self.confirmed:
                await self._test_pair()
        elif word in ("/setup", "/cancel_setup"):
            if self._wizard is None:
                await self._reply("This install uses a manual profiles file. See the Docker guide.")
            elif self._setup_allowed(msg.get("chat"), msg.get("from")):
                setup_reply = (
                    await self._wizard.begin() if word == "/setup" else await self._wizard.cancel()
                )
                await self._reply(setup_reply.text, setup_reply.markup)
            else:
                await self._reply("Open the private setup link or confirm both test alerts first.")
        elif word == "/test":
            await self._test_pair()
            # the phone pair and its button stay the rule-16 proof; a webhook only needs a 2xx
            if self._webhook_test is not None:
                await self._reply(render_webhook_tests(await self._webhook_test()))
        elif word == "/status":
            view = self._rt.view()
            await self._reply(
                render_status(view, self.confirmed), status_buttons(view, self.confirmed)
            )
        elif word == "/trip":
            if _obj(msg.get("chat")).get("type") == "private":
                await self._check_trip(text.split(maxsplit=1)[1:])
        elif word == "/arrived":
            await self._arrival_review(args)
        elif word in ("/stats", "/clearstats"):
            await self._statistics(word, args)
        elif word == "/stations":
            await self._reply(stations_text(self._rt.view()))
        elif word == "/forget":
            await self._forget(args)
        elif word in ("/choose", "/leaving", "/skip"):
            await self._targeted(word, args)
        elif word in ("/plan", "/plans", "/agents", "/preferences", "/remember", "/forgetpref"):
            if not self.confirmed:
                await self._reply("Confirm phone delivery with /test before managing travel plans.")
                return
            async with self._travel_lock:
                if word == "/agents":
                    reply = self._agent_access()
                elif word in ("/plan", "/plans"):
                    reply = self._travel_chat.command(word)
                else:
                    reply = self._travel_chat.memory_command(word, " ".join(args))
                if await self._reply(reply.text, reply.markup) and word in (
                    "/plans",
                    "/preferences",
                ):
                    await self._offer_travel_memory(check=True)
        elif self._wizard is not None and self._wizard.pending and not word.startswith("/"):
            if self._setup_allowed(msg.get("chat"), msg.get("from")):
                setup_reply = await self._wizard.text(text)
                await self._reply(setup_reply.text, setup_reply.markup)
        elif self.confirmed and not text.lstrip().startswith("/"):
            async with self._travel_lock:
                reply_or_none = self._travel_chat.text(text)
                if reply_or_none is None:
                    reply_or_none = await self._travel_chat.interpret(text)
                if reply_or_none and await self._reply(reply_or_none.text, reply_or_none.markup):
                    await self._offer_travel_memory()

    async def _check_trip(self, args: list[str]) -> None:
        language = shared_language(self._rt.view())
        raw = args[0] if args else ""
        words = raw.split(maxsplit=1)
        mode = words[0].casefold() if words else ""
        if len(words) == 2 and mode in ("saved", "reverse"):
            preferences = self.travel_actions.preferences
            saved = preferences.saved_route(words[1]) if preferences is not None else None
            if saved is None:
                await self._reply(localized("trip.saved_missing", language))
                return
            pair = list(reversed(saved) if mode == "reverse" else saved)
        else:
            pair = raw.split("->") if "->" in raw else raw.split()
        if len(pair) != 2 or not all(station.strip() for station in pair):
            await self._reply(localized("command.trip_usage", language))
            return
        result = await self._call_runtime(self._rt.check_trip(*pair))
        await self._reply(result.text if isinstance(result, Refused) else result["text"])

    async def _offer_travel_memory(self, *, check: bool = False) -> None:
        reply = self._travel_chat.memory_suggestion(check=check)
        if reply is not None and await self._reply(reply.text, reply.markup):
            self._travel_chat.suggestion_delivered()

    async def _statistics(self, word: str, args: Sequence[str]) -> None:
        if not self.confirmed:
            await self._reply("Confirm phone delivery with /test before using commute statistics.")
            return
        if word == "/clearstats":
            token = secrets.token_hex(12)
            keys = frozenset(self._rt.state.commute_history)
            self._stats_clear = (token, self._rt.now().astimezone(UTC), keys)
            await self._reply(
                f"Delete all {len(keys)} local commute statistics rows and any queued recaps or trip notes?",
                buttons((("Delete these statistics", f"stats|{token}"),)),
            )
            return
        days = STATS_PERIODS.get(args[0].lower() if args else "month")
        if days is None or len(args) > 1:
            await self._reply("Use /stats week, /stats month, or /stats year.")
            return
        language = shared_language(self._rt.view())
        await self._reply(
            localized("stats.period", language, days=days)
            + "\n"
            + stats_text(self._rt.commute_stats(days), language)
        )

    def _statistics_delete(self, token: str) -> Outcome:
        draft = self._stats_clear
        if (
            not self.confirmed
            or draft is None
            or token != draft[0]
            or self._rt.now().astimezone(UTC) - draft[1] > MAX_AGE
        ):
            return Outcome(INVALID)
        self._stats_clear = None
        if self._rt.state.commute_history.keys() != draft[2]:
            return Outcome("Statistics changed. Use /clearstats to review the current rows.")
        if not self._rt.clear_commute_history():
            return Outcome("Statistics could not be cleared. Use /clearstats to retry.")
        return Outcome("Local commute statistics and their queued notes were deleted.", done=True)

    def _agent_access(self) -> TravelReply:
        policies = self.travel_actions.policies()
        if not policies:
            return TravelReply("There are no registered agents.")
        text = "Plan change access\n" + "\n".join(
            f"{agent}: {POLICY_LABELS[policy]}" for agent, policy in policies.items()
        )
        return TravelReply(text, buttons(*[[(agent, f"ag|{agent}")] for agent in policies]))

    async def prompt_travel_approval(self, text: str, approval_id: str) -> bool:
        rows = [[("Allow once", f"ta|{approval_id}|once"), ("Deny", f"ta|{approval_id}|deny")]]
        if self.travel_actions.can_always(approval_id):
            rows.append([("Always allow", f"ta|{approval_id}|always")])
        return self.confirmed and await self._reply(text, buttons(*rows))

    def _setup_allowed(self, chat: Any, sender: Any) -> bool:
        return (
            self._state_safe
            and self._chat is not None
            and _obj(chat).get("type") == "private"
            and _obj(sender).get("id") == self._chat
            and (self.confirmed or self.state.claimed_for == f"{self._api.bot_id}:{self._chat}")
        )

    async def _targeted(self, word: str, args: Sequence[str]) -> None:
        view = self._rt.view()  # outside the try: only parse_target's refusals are replies
        try:
            t = parse_target(view, args)
        except ValueError as e:
            await self._reply(str(e))
            return
        name = display_name(t.profile_name)
        if word == "/choose":
            if t.departure is None:
                await self._reply(
                    localized("command.choose_usage", profile_language(view, t.profile_id))
                )
                return
            out = await self._choose(
                t.profile_id, name, t.departure, t.train, t.service_date, t.direction
            )
            await self._reply(out.text)
        elif word == "/leaving":
            await self._reply((await self._leaving(t.profile_id, t.service_date, t.direction)).text)
        else:
            whole = t.whole_day or t.direction is None
            await self._reply(
                (await self._skip(t.profile_id, t.service_date, t.direction, whole)).text
            )

    async def _button(self, cb: dict[str, Any]) -> None:
        """Every tap is answered with the tapped button's label and what it led to. A tap that
        took effect also removes the used button from its message (a one-time choice: the whole
        keyboard); a refused or failed tap leaves the keyboard as it was."""
        try:
            await self._api.call("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        except TelegramError as e:
            log.warning("Telegram answerCallbackQuery failed (%s)", e.code)
        if self._cursor.closed:
            return
        data, message = cb.get("data"), _obj(cb.get("message"))
        try:
            parts = data.split("|") if isinstance(data, str) else []
            if parts[:1] == ["setup"]:
                if (
                    len(parts) != 3
                    or self._wizard is None
                    or not self._setup_allowed(message.get("chat"), cb.get("from"))
                ):
                    out = Outcome(INVALID)
                else:
                    reply = await self._wizard.button(parts[1], parts[2])
                    out = Outcome(reply.text, reply.markup, reply.done)
            else:
                out = await self._dispatch(data, cb.get("id"))
        except _TimedOut:
            out = Outcome(TIMED_OUT)
        except Exception:
            log.exception("Telegram button failed")
            out = Outcome(FAILED)
        label = _label(message, data)
        text = out.text if out.text.startswith(label + ":") else f"{label}: {out.text}"
        replied = await self._reply(text, out.markup)
        if out.done and replied:
            tag = data.split("|", 1)[0] if isinstance(data, str) else ""
            await self._retire(message, None if tag in WHOLE_KEYBOARD else data)
            if tag in ("ta", "tp"):
                async with self._travel_lock:
                    await self._offer_travel_memory(check=tag == "ta" and out.markup is not None)

    async def _retire(self, message: dict[str, Any], data: str | None) -> None:
        # Callback snapshots can predate another tap's edit, so edits share retired controls.
        async with self._keyboard_lock:
            await self._retire_locked(message, data)

    async def _retire_locked(self, message: dict[str, Any], data: str | None) -> None:
        mid = message.get("message_id")
        if type(mid) is not int or self._cursor.closed:
            log.info("no Telegram message to remove the used button from")
            return
        rows: list[Any] = []
        retired = self._retired.setdefault(mid, set())
        if data is not None and retired is not None:
            retired.add(_button_key(data))
            old = _obj(message.get("reply_markup")).get("inline_keyboard")
            old = old if isinstance(old, list) else []
            rows = [
                kept
                for row in old
                if isinstance(row, list)
                and (
                    kept := [
                        b for b in row if _button_key(_obj(b).get("callback_data")) not in retired
                    ]
                )
            ]
            if rows == old:  # no keyboard, or the button is not on it: wiping it would lose others
                log.info("the used button is not on its Telegram message's keyboard")
                return
        else:
            self._retired[mid] = None
        try:  # handle() checked this message's chat is self._chat
            await self._api.call(
                "editMessageReplyMarkup",
                {
                    "chat_id": self._chat,
                    "message_id": mid,
                    "reply_markup": {"inline_keyboard": rows},
                },
            )
        except TelegramError as e:
            log.info("Telegram keyboard removal failed (%s: %s)", e.code, e)

    async def _dispatch(self, data: Any, action_id: Any) -> Outcome:
        parts = data.split("|") if isinstance(data, str) else []
        if parts[:1] and parts[0] in ("tp", "ta", "ag", "tm"):
            if not self.confirmed:
                return Outcome("Confirm phone delivery with /test before managing travel plans.")
            if parts[0] in ("ta", "ag"):
                travel = self._travel_button(parts)
            else:
                async with self._travel_lock:
                    travel = self._travel_button(parts)
            return Outcome(travel.text, travel.markup, travel.done)
        if parts[:1] == ["av"] and len(parts) == 2:
            return await self._arrival_confirm(parts[1])
        if parts[:1] == ["stats"] and len(parts) == 2:
            return self._statistics_delete(parts[1])
        if parts[:1] == ["ap"] and len(parts) == 3:
            return await self._arrival_answer(parts[1], parts[2])
        if parts[:1] == ["q"] and len(parts) == 3:
            return await self._answer(parts[1], parts[2])
        if parts[:1] == ["ok"] and len(parts) == 2:
            return await self._confirm(parts[1])
        if parts[:1] and parts[0] in {"rp", "ra", "rf", "rc"}:
            return await self._recovery_button(parts, action_id)
        try:
            tag, pid, d, day, *rest = parts
            direction, service_date = Direction(d), date.fromisoformat(day)
            at = time.fromisoformat(rest[0]) if tag == "c" and rest else None
        except ValueError:
            return Outcome(INVALID)
        names = {x.profile_id: x.profile_name for x in self._rt.view().directions}
        name = display_name(names.get(pid, pid))
        if tag == "l" and not rest:
            return await self._leaving(pid, service_date, direction)
        if tag == "s" and not rest:
            return await self._skip(pid, service_date, direction, False)
        if at is not None and len(rest) <= 2:
            return await self._choose(pid, name, at, rest[1] if len(rest) == 2 else None,
                                      service_date, direction)  # fmt: skip
        return Outcome(INVALID)

    def _travel_button(self, parts: list[str]) -> TravelReply:
        tag, *args = parts
        if tag in ("tp", "tm"):
            return self._travel_chat.button("|".join(parts))
        if tag == "ta" and len(args) == 2:
            return plan_receipt(self.travel_actions.decide(args[0], args[1]))
        if tag == "ag" and args and args[0] in self.travel_actions.policies():
            agent = args[0]
            if len(args) == 1:
                return TravelReply(
                    f"Which plan changes may {agent} make?",
                    buttons(
                        (("Ask", f"ag|{agent}|ask"), ("Always allow", f"ag|{agent}|always")),
                        (("Deny", f"ag|{agent}|deny"),),
                    ),
                    done=True,
                )
            if len(args) == 2:
                return plan_receipt(self.travel_actions.set_policy(agent, args[1]))
        return TravelReply(INVALID)

    async def _arrival_review(self, args: Sequence[str]) -> None:
        if not self.confirmed:
            await self._reply("Confirm phone delivery with /test before recording an arrival.")
            return
        try:
            draft = parse_arrival(args)
        except ValueError as error:
            choices = ""
            if not args:
                choices = "\nSaved journeys:\n" + "\n".join(
                    f"{r.key.profile_id} {r.key.direction.value} {r.key.service_date} {r.destination}"
                    for r in self._rt.arrival_records()[:12]
                )
            await self._reply(str(error) + choices)
            return
        record = next((r for r in self._rt.arrival_records() if r.key == draft.key), None)
        if record is None or record.destination != draft.destination:
            await self._reply(
                "No saved journey matches that profile, direction, service date and destination."
            )
            return
        now = self._rt.now()
        try:
            check_arrival(
                record,
                draft.destination,
                draft.at,
                now=now,
                expected_revision=record.revision,
                action_id="telegram-review",
                original_planned_arrival=draft.original_planned_arrival,
            )
        except ArrivalError as error:
            await self._reply(str(error))
            return
        self._arrival_drafts = {
            k: v
            for k, v in self._arrival_drafts.items()
            if v.phase_at(now, MAX_AGE) is ReviewPhase.READY
        }
        if len(self._arrival_drafts) >= 16:
            self._arrival_drafts.pop(next(iter(self._arrival_drafts)))
        nonce = secrets.token_hex(12)
        self._arrival_drafts[nonce] = ArrivalDraft(draft, record.revision, now)
        plan = record.plan.planned_arrival if record.plan else draft.original_planned_arrival
        await self._reply(
            f"Review arrival for {draft.key.profile_id}, {draft.key.direction.value}, "
            f"service date {draft.key.service_date}: destination {draft.destination}; "
            f"actual arrival {draft.at.isoformat()}; original planned arrival "
            f"{plan.isoformat() if plan else 'missing, enter planned= with your original timetable'}. "
            "This is your explicit arrival record. It does not decide compensation eligibility.",
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "Confirm this destination and arrival time",
                            "callback_data": f"av|{nonce}",
                        }
                    ]
                ]
            },
        )

    async def _arrival_confirm(self, nonce: str) -> Outcome:
        saved = self._arrival_drafts.get(nonce)
        if not self.confirmed or saved is None:
            return Outcome("That arrival review is old. Enter /arrived again.")
        if saved.phase_at(self._rt.now(), MAX_AGE) is ReviewPhase.EXPIRED:
            self._arrival_drafts.pop(nonce, None)
            return Outcome("That arrival review is old. Enter /arrived again.")
        draft, revision = saved.arrival, saved.revision
        result = await self._call_runtime(
            self._rt.confirm_arrival(
                draft.key,
                draft.destination,
                draft.at,
                expected_revision=revision,
                action_id=f"telegram-arrival:{nonce}",
                original_planned_arrival=draft.original_planned_arrival,
            )
        )
        if isinstance(result, Refused):
            return Outcome(result.text)
        language = profile_language(self._rt.view(), result.key.profile_id)
        return Outcome(
            localized("arrival.saved", language) + arrival_text(result, language), done=True
        )

    async def _arrival_answer(self, nonce: str, answer: str) -> Outcome:
        recipient = self.arrival_recipient
        if recipient is None:
            return Outcome("Confirm phone delivery with /test before recording an arrival.")
        result = await self._call_runtime(self._rt.answer_arrival_prompt(nonce, answer, recipient))
        if isinstance(result, Refused):
            return Outcome(result.text)
        if answer == "other":
            key = result.key
            command = (
                f"/arrived {key.profile_id} {key.direction.value} "
                f"{key.service_date} {result.destination} "
            )
            return Outcome(
                "Enter your actual arrival as a full date and time with UTC offset:\n"
                f"{command}<arrival-time>\nThen review and confirm it.",
                done=True,
            )
        language = profile_language(self._rt.view(), result.key.profile_id)
        return Outcome(
            localized("arrival.saved", language) + arrival_text(result, language), done=True
        )

    async def _confirm(self, nonce: str) -> Outcome:
        proof = self._proof
        if proof is not None and proof.claim(nonce):
            saved = await self._save(f"{self._api.bot_id}:{self._chat}", nonce)
            proof.saved(saved)
            if saved:
                return Outcome("Delivery confirmed. Setup is complete.", done=True)
            return Outcome("Delivery proof was not saved. Tap the confirmation again to retry.")
        if proof is not None and proof.matches(nonce) and proof.phase is ProofPhase.SAVING:
            return Outcome("Delivery confirmation is being saved.")  # a double tap
        accepted = self.state.accepted_nonce
        if self.confirmed and accepted is not None and DeliveryProof(accepted).matches(nonce):
            # a second tap on the test that proved it, also after a restart or a later /test;
            # a no-op, so its keyboard stays
            return Outcome("Delivery is already confirmed.")
        return Outcome("That test is old; send /test again.")

    # --- actions ---

    async def _recovery_button(self, parts: list[str], action_id: Any) -> Outcome:
        if not self.confirmed:
            return Outcome("Confirm phone delivery with /test before selecting a recovery.")
        stale = Outcome("That recovery button is old; open /status again.")
        if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
            return stale
        view = self._rt.view()
        occurrences = [o for o in view.occurrences if not o.resolved and not o.not_travelling]
        tag = parts[0]
        if (
            tag in {"rp", "ra"}
            and len(parts) == (2 if tag == "rp" else 3)
            and re.fullmatch(r"[0-9a-f]{32}", parts[1])
        ):
            plans = [
                (o, p)
                for o in occurrences
                for p in o.replacement_plans
                if p.selectable and p.proposal_id == parts[1]
            ]
            if len(plans) == 1:
                occurrence, plan = plans[0]
                language = profile_language(view, occurrence.key.profile_id)
                if tag == "rp":
                    return Outcome(*recovery_origin_prompt(plan, language))
                if re.fullmatch(r"[0-9]{1,4}", parts[2]) and int(parts[2]) in recovery_origins(
                    plan
                ):
                    origin = plan.itinerary.steps[int(parts[2])].origin
                    offer = await self._call_runtime(
                        self._rt.prepare_recovery(
                            occurrence.key, plan.proposal_id, origin.physical_id
                        )
                    )
                    if isinstance(offer, Refused):
                        return Outcome(offer.text)
                    return Outcome(
                        render_recovery_offer(offer, language),
                        recovery_offer_buttons(offer, language),
                        done=True,
                    )
        elif tag == "rf" and len(parts) == 2 and re.fullmatch(r"[0-9a-f]{24}", parts[1]):
            offers = [
                (o, o.recovery_offer)
                for o in occurrences
                if o.recovery_offer is not None and o.recovery_offer.id == parts[1]
            ]
            if len(offers) == 1:
                occurrence, offer = offers[0]
                language = profile_language(view, occurrence.key.profile_id)
                selected = await self._call_runtime(
                    self._rt.select_recovery(
                        occurrence.key,
                        offer.id,
                        acknowledge_planned_times=True,
                        acknowledge_accessibility_unknown=True,
                    )
                )
                if isinstance(selected, Refused):
                    return Outcome(selected.text)
                return Outcome(
                    render_selected_recovery(selected, language),
                    recovery_progress_buttons(selected, language),
                    done=True,
                )
        elif (
            tag == "rc"
            and len(parts) == 5
            and re.fullmatch(r"[0-9a-f]{32}", parts[1])
            and all(re.fullmatch(r"[0-9]{1,9}", p) for p in parts[2:4])
        ):
            selections = [
                (o, o.selected_recovery)
                for o in occurrences
                if o.selected_recovery is not None and o.selected_recovery.id == parts[1]
            ]
            kinds: dict[str, RecoveryActionKind] = {
                "b": "boarded",
                "a": "arrived",
                "w": "walk_arrived",
                "s": "stop",
            }
            kind = kinds.get(parts[4])
            if len(selections) == 1 and kind is not None:
                occurrence, selected = selections[0]
                language = profile_language(view, occurrence.key.profile_id)
                revision, index = int(parts[2]), int(parts[3])
                duplicate = any(
                    a.id == action_id
                    and a.revision == revision
                    and a.step_index == index
                    and a.kind == kind
                    for a in selected.confirmations
                )
                current = selected.status == "active" and (selected.revision, selected.index) == (
                    revision,
                    index,
                )
                if (current or duplicate) and 0 <= index < len(selected.itinerary.steps):
                    if kind == "stop":
                        result = await self._call_runtime(
                            self._rt.stop_recovery(occurrence.key, selected.id, revision)
                        )
                    elif duplicate or kind == selected.next_action:
                        step = selected.itinerary.steps[index]
                        place = step.origin if kind == "boarded" else step.destination
                        result = await self._call_runtime(
                            self._rt.confirm_recovery_step(
                                occurrence.key,
                                selected.id,
                                revision,
                                index,
                                kind,
                                place.physical_id,
                                action_id,
                            )
                        )
                    else:
                        return stale
                    if isinstance(result, Refused):
                        return Outcome(result.text)
                    return Outcome(
                        render_selected_recovery(result, language),
                        recovery_progress_buttons(result, language),
                        done=True,
                    )
        return stale

    async def _choose(
        self, pid: str, name: str, at: time, train: str | None,
        day: date | None, direction: Direction | None,
    ) -> Outcome:  # fmt: skip
        refused = await self._call_runtime(self._rt.choose_at(pid, at, train, day, direction))
        if refused is not None:
            return Outcome(refused.text)
        language = profile_language(self._rt.view(), pid)
        return Outcome(
            localized("command.locked", language, time=f"{at:%H:%M}", name=name), done=True
        )

    async def _answer(self, prompt_id: str, answer: str) -> Outcome:
        """A button on a question; TCC's own next message follows, a refusal says why not."""
        refused = await self._call_runtime(self._rt.answer_prompt(prompt_id, answer))
        if refused is not None:
            return Outcome(refused.text)
        return Outcome(
            localized("command.answer_saved", shared_language(self._rt.view())), done=True
        )

    async def _forget(self, args: Sequence[str]) -> None:
        language = shared_language(self._rt.view())
        if len(args) != 1:
            await self._reply(localized("command.forget_usage", language))
            return
        code, done, failed = args[0].upper(), 0, None
        for info in self._rt.view().directions:
            forgot = self._rt.forget_station(info.profile_id, info.direction, code)
            refused = await self._call_runtime(forgot)
            done += refused is None
            if refused is not None and refused.kind is RefusalKind.RETRY:
                failed = refused  # still remembered there: say so, never "not remembered"
        if failed is not None:
            await self._reply(
                localized("command.forgot_some", language, station=code, error=failed.text)
                if done
                else failed.text
            )
            return
        await self._reply(
            localized(
                "command.forgot" if done else "command.station_unsaved", language, station=code
            )
        )

    async def _leaving(self, pid: str, day: date | None, direction: Direction | None) -> Outcome:
        refused = await self._call_runtime(self._rt.leaving(pid, day, direction))
        if refused is not None:
            return Outcome(_nothing(refused))
        language = profile_language(self._rt.view(), pid)
        return Outcome(localized("command.leaving", language), done=True)

    async def _skip(
        self, pid: str, day: date | None, direction: Direction | None, whole: bool
    ) -> Outcome:
        refused = await self._call_runtime(
            self._rt.not_travelling(pid, day, direction, whole_day=whole)
        )
        if refused is not None:
            return Outcome(_nothing(refused))
        language = profile_language(self._rt.view(), pid)
        return Outcome(
            localized("command.skip_day" if whole else "command.skip_trip", language), done=True
        )

    async def _call_runtime(self, aw: Awaitable[T]) -> T:
        """The runtime call is its own task: a timeout or a cancelled handler leaves it to finish
        (a cancelled delivery pass after a send would repeat that alert)."""
        task = asyncio.ensure_future(aw)
        self._core.add(task)
        task.add_done_callback(self._core.discard)
        call = DetachedCall(
            task, lambda error: log.error("detached command failed", exc_info=error)
        )
        try:
            return await call.wait(COMMAND_TIMEOUT_S)
        except TimeoutError:
            raise _TimedOut from None

    # --- sending ---

    async def _test_pair(self) -> None:
        if self._cursor.phase is CursorPhase.CLOSED:
            return
        nonce = secrets.token_hex(4)
        proof = self._proof = DeliveryProof(nonce)
        language = shared_language(self._rt.view())
        button = {
            "inline_keyboard": [
                [
                    {
                        "text": localized("command.test_both", language),
                        "callback_data": f"ok|{nonce}",
                    }
                ]
            ]
        }
        try:
            await self._send(localized("command.test_loud", language), disable_notification=False)
            await self._send(
                localized("command.test_silent", language),
                disable_notification=True,
                reply_markup=button,
            )
        except TelegramError as e:
            proof.phase = ProofPhase.FAILED
            log.warning("Telegram test alert failed (%s: %s); send /test again", e.code, e)
            return
        if self._proof is proof:
            proof.arm()

    async def _reply(self, text: str, markup: dict[str, Any] | None = None) -> bool:
        parts = split_text(text)
        try:
            for i, part in enumerate(parts):
                extra = {"reply_markup": markup} if markup and i == len(parts) - 1 else {}
                await self._send(part, **extra)
        except TelegramError as e:
            log.warning("Telegram reply failed (%s: %s)", e.code, e)
            return False
        return not self._cursor.closed

    async def _send(self, text: str, **params: Any) -> None:
        if self._cursor.phase is CursorPhase.CLOSED:
            return
        await self._api.call("sendMessage", {"chat_id": self._chat, "text": text, **params})


def _nothing(refused: Refused) -> str:
    return f"Nothing to act on: {refused.text}."


def _button_key(data: Any) -> Any:
    # A recovery action invalidates every control for the same selection, revision and step.
    return data.rsplit("|", 1)[0] if isinstance(data, str) and data.startswith("rc|") else data


def _label(message: dict[str, Any], data: Any) -> str:
    """The tapped button's own text from its message's keyboard, else a short one for its tag."""
    rows = _obj(message.get("reply_markup")).get("inline_keyboard")
    for row in rows if isinstance(rows, list) else []:
        for button in row if isinstance(row, list) else []:
            text = _obj(button).get("text")
            if _obj(button).get("callback_data") == data and isinstance(text, str) and text:
                return text
    tag = data.split("|", 1)[0] if isinstance(data, str) else ""
    return BUTTON_LABELS.get(tag, "Button")


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _updates(v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        return []
    updates: dict[int, dict[str, Any]] = {}
    for update in v:
        if isinstance(update, dict) and type(uid := update.get("update_id")) is int and uid >= 0:
            updates.setdefault(uid, update)
    return [updates[uid] for uid in sorted(updates)]

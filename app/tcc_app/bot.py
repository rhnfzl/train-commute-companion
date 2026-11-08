"""The Telegram bot: commands, buttons, the delivery test and the chat allow-list (spec 9 step 3,
7.12 rules 15 and 16). Thin: every train decision is the core's."""

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, time, timedelta
from pathlib import Path
from typing import Any, TypeVar

from train_commute_companion.compensation import ArrivalError, arrival_text
from train_commute_companion.compensation import confirm as check_arrival
from train_commute_companion.location import Fix, number
from train_commute_companion.messages import Refused
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
    UpdateCursor,
)
from .commands import (
    command_word,
    parse_arrival,
    parse_target,
    recovery_offer_buttons,
    recovery_origin_prompt,
    recovery_origins,
    recovery_progress_buttons,
    render_recovery_offer,
    render_selected_recovery,
    render_status,
    render_webhook_tests,
    status_buttons,
)
from .follow import FUTURE_LIMIT, Follower, stamp
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
    "/arrived: enter and review an actual destination arrival with a full date and timezone\n"
    "/skip [profile] [outbound|return|day] [YYYY-MM-DD]: not travelling, alerts for it stop\n"
    "/test: send the loud and the silent test alert (and test each webhook)\n"
    "/help: this list\n"
    "Data: NS"
)


@dataclass(slots=True)
class ShellState:
    offset: int = 0
    bot_id: str | None = None  # the bot whose update ids `offset` counts
    confirmed_for: str | None = None  # "<bot id>:<chat id>" (rule 16 proves one target)


def load_state(path: Path) -> ShellState | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        s = ShellState(**raw)
        if (
            isinstance(s.offset, bool)
            or not isinstance(s.offset, int)
            or s.offset < 0
            or not all(v is None or isinstance(v, str) for v in (s.bot_id, s.confirmed_for))
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
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(asdict(s)))
            f.flush()
            os.fsync(f.fileno())  # a power loss must not leave an empty file behind the rename
        os.replace(tmp, path)
    except OSError:
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
    ) -> None:
        self._api, self._rt, self._chat, self._path = api, rt, chat_id, state_path
        self._follower, self._webhook_test = follower, webhook_test
        loaded = load_state(state_path)
        trusted = loaded is not None and loaded.bot_id == api.bot_id
        self._cursor = UpdateCursor(CursorPhase.READY if trusted else CursorPhase.UNTRUSTED)
        if loaded is not None and trusted:
            self.state = loaded
        else:  # another bot's or no cursor: only the confirmation (it names its bot) is kept
            self.state = ShellState(confirmed_for=loaded.confirmed_for if loaded else None)
        self._proof: DeliveryProof | None = None
        self._arrival_drafts: dict[str, ArrivalDraft] = {}
        self._save_phase = SavePhase.READY
        self._save_lock = asyncio.Lock()  # one save at a time: no shared .tmp, the newest wins
        # saves: drained by close(), never cancelled (serve's cleanup leaves them out too)
        self.writes: set[asyncio.Task[bool]] = set()
        self._tasks: set[asyncio.Task[None]] = set()  # handlers, cancelled by close()
        self._core: set[asyncio.Task[Any]] = set()  # runtime calls, never cancelled here

    @property
    def confirmed(self) -> bool:
        return self.state.confirmed_for == f"{self._api.bot_id}:{self._chat}"

    # --- polling ---

    async def run(self) -> None:
        while not self._cursor.closed:
            try:
                if self._cursor.phase is CursorPhase.UNTRUSTED:
                    await self._discard_backlog()
                if self._cursor.phase is CursorPhase.CLOSED:
                    return
                ups = await self._api.call(
                    "getUpdates",
                    {"offset": self.state.offset, "timeout": POLL_S, "allowed_updates": ALLOWED},
                    timeout=POLL_S + 10,
                )
                await self._commit_and_dispatch(_updates(ups))
            except TelegramError as e:
                log.warning("Telegram getUpdates failed (%s: %s)", e.code, e)
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

    async def _save(self, confirmed_for: str | None = None) -> bool:
        # a cancelled caller leaves the write running and the lock held until its worker ends
        task = asyncio.ensure_future(self._write(confirmed_for))
        self.writes.add(task)
        task.add_done_callback(self.writes.discard)
        return await asyncio.shield(task)

    async def _write(self, confirmed_for: str | None = None) -> bool:
        async with self._save_lock:
            snap = replace(self.state)  # the worker writes this copy, never a state being changed
            if confirmed_for is not None:
                snap.confirmed_for = confirmed_for
            try:
                await asyncio.to_thread(save_state, self._path, snap)  # off the loop (rule 12)
            except OSError as e:
                if self._save_phase is SavePhase.READY:
                    log.warning("cannot save Telegram state (%s); retrying", type(e).__name__)
                self._save_phase = SavePhase.FAILED
                return False
            if confirmed_for is not None:
                self.state.confirmed_for = confirmed_for
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
        chat = _obj((msg or edited or _obj(cb.get("message"))).get("chat")).get("id")
        if self._chat is None or chat != self._chat:
            _log_foreign_chat(chat)
            return
        if not cb and "location" in (msg or edited):
            self._location(msg or edited)  # no command: a failure is logged, never answered
            return
        try:
            if cb:
                await self._button(cb)
            elif msg and isinstance(msg.get("text"), str):
                await self._message(msg)
            # an edited message is read only for its location (live location moves edit it)
        except _TimedOut:
            await self._reply("NS did not answer in time; try again.")
        except Exception:
            # the exception text stays out of the chat; without a reply the user cannot tell
            # whether a skip or a lock took effect
            log.exception("Telegram command failed")
            await self._reply("That failed; see the app log.")

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
            await self._reply(HELP)
            if not self.confirmed:
                await self._test_pair()
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
        elif word == "/arrived":
            await self._arrival_review(args)
        elif word in ("/choose", "/leaving", "/skip"):
            await self._targeted(word, args)

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
                await self._reply(USAGE_CHOOSE)
                return
            await self._choose(
                t.profile_id, name, t.departure, t.train, t.service_date, t.direction
            )
        elif word == "/leaving":
            await self._leaving(t.profile_id, t.service_date, t.direction)
        else:
            whole = t.whole_day or t.direction is None
            await self._skip(t.profile_id, t.service_date, t.direction, whole)

    async def _button(self, cb: dict[str, Any]) -> None:
        try:
            await self._api.call("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        except TelegramError as e:
            log.warning("Telegram answerCallbackQuery failed (%s)", e.code)
        if self._cursor.closed:
            return
        data = cb.get("data")
        parts = data.split("|") if isinstance(data, str) else []
        if parts[:1] == ["av"] and len(parts) == 2:
            await self._arrival_confirm(parts[1])
            return
        if parts[:1] == ["ok"] and len(parts) == 2:
            await self._confirm(parts[1])
            return
        if parts[:1] and parts[0] in {"rp", "ra", "rf", "rc"}:
            await self._recovery_button(parts, cb.get("id"))
            return
        try:
            tag, pid, d, day, *rest = parts
            direction, service_date = Direction(d), date.fromisoformat(day)
            at = time.fromisoformat(rest[0]) if tag == "c" and rest else None
        except ValueError:  # unparseable: answered, ignored
            return
        names = {x.profile_id: x.profile_name for x in self._rt.view().directions}
        name = display_name(names.get(pid, pid))
        if tag == "l" and not rest:
            await self._leaving(pid, service_date, direction)
        elif tag == "s" and not rest:
            await self._skip(pid, service_date, direction, False)
        elif at is not None and len(rest) <= 2:
            await self._choose(pid, name, at, rest[1] if len(rest) == 2 else None,
                               service_date, direction)  # fmt: skip

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

    async def _arrival_confirm(self, nonce: str) -> None:
        saved = self._arrival_drafts.get(nonce)
        if not self.confirmed or saved is None:
            await self._reply("That arrival review is old. Enter /arrived again.")
            return
        if saved.phase_at(self._rt.now(), MAX_AGE) is ReviewPhase.EXPIRED:
            self._arrival_drafts.pop(nonce, None)
            await self._reply("That arrival review is old. Enter /arrived again.")
            return
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
        await self._reply(
            result.text if isinstance(result, Refused) else "Arrival saved. " + arrival_text(result)
        )

    async def _confirm(self, nonce: str) -> None:
        proof = self._proof
        if proof is not None and proof.claim(nonce):
            accepted = await self._save(f"{self._api.bot_id}:{self._chat}")
            proof.saved(accepted)
            await self._reply(
                "Delivery confirmed. Setup is complete."
                if accepted
                else "Delivery proof was not saved. Tap the confirmation again to retry."
            )
        else:
            await self._reply("That test is old; send /test again.")

    # --- actions ---

    async def _recovery_button(self, parts: list[str], action_id: Any) -> None:
        if not self.confirmed:
            await self._reply("Confirm phone delivery with /test before selecting a recovery.")
            return
        stale = "That recovery button is old; open /status again."
        if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
            await self._reply(stale)
            return
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
                if tag == "rp":
                    await self._reply(*recovery_origin_prompt(plan))
                    return
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
                        await self._reply(offer.text)
                    else:
                        await self._reply(
                            render_recovery_offer(offer), recovery_offer_buttons(offer)
                        )
                    return
        elif tag == "rf" and len(parts) == 2 and re.fullmatch(r"[0-9a-f]{24}", parts[1]):
            offers = [
                (o, o.recovery_offer)
                for o in occurrences
                if o.recovery_offer is not None and o.recovery_offer.id == parts[1]
            ]
            if len(offers) == 1:
                occurrence, offer = offers[0]
                selected = await self._call_runtime(
                    self._rt.select_recovery(
                        occurrence.key,
                        offer.id,
                        acknowledge_planned_times=True,
                        acknowledge_accessibility_unknown=True,
                    )
                )
                if isinstance(selected, Refused):
                    await self._reply(selected.text)
                else:
                    await self._reply(
                        render_selected_recovery(selected), recovery_progress_buttons(selected)
                    )
                return
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
                        await self._reply(stale)
                        return
                    if isinstance(result, Refused):
                        await self._reply(result.text)
                    else:
                        await self._reply(
                            render_selected_recovery(result), recovery_progress_buttons(result)
                        )
                    return
        await self._reply(stale)

    async def _choose(
        self, pid: str, name: str, at: time, train: str | None,
        day: date | None, direction: Direction | None,
    ) -> None:  # fmt: skip
        refused = await self._call_runtime(self._rt.choose_at(pid, at, train, day, direction))
        await self._reply(f"Locked the {at:%H:%M} for {name}." if refused is None else refused.text)

    async def _leaving(self, pid: str, day: date | None, direction: Direction | None) -> None:
        refused = await self._call_runtime(self._rt.leaving(pid, day, direction))
        await self._reply("Marked as leaving now." if refused is None else _nothing(refused))

    async def _skip(
        self, pid: str, day: date | None, direction: Direction | None, whole: bool
    ) -> None:
        refused = await self._call_runtime(
            self._rt.not_travelling(pid, day, direction, whole_day=whole)
        )
        if refused is not None:
            await self._reply(_nothing(refused))
        elif whole:
            await self._reply("Not travelling today: alerts for both directions stop.")
        else:
            await self._reply("Not travelling: alerts for that journey stop.")

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
        button = {"inline_keyboard": [[{"text": "I got both", "callback_data": f"ok|{nonce}"}]]}
        try:
            await self._send(LOUD_TEST, disable_notification=False)
            await self._send(SILENT_TEST, disable_notification=True, reply_markup=button)
        except TelegramError as e:
            proof.phase = ProofPhase.FAILED
            log.warning("Telegram test alert failed (%s: %s); send /test again", e.code, e)
            return
        if self._proof is proof:
            proof.arm()

    async def _reply(self, text: str, markup: dict[str, Any] | None = None) -> None:
        parts = split_text(text)
        try:
            for i, part in enumerate(parts):
                extra = {"reply_markup": markup} if markup and i == len(parts) - 1 else {}
                await self._send(part, **extra)
        except TelegramError as e:
            log.warning("Telegram reply failed (%s: %s)", e.code, e)

    async def _send(self, text: str, **params: Any) -> None:
        if self._cursor.phase is CursorPhase.CLOSED:
            return
        await self._api.call("sendMessage", {"chat_id": self._chat, "text": text, **params})


def _nothing(refused: Refused) -> str:
    return f"Nothing to act on: {refused.text}."


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

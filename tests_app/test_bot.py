import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from builders import AMS, ONE_TRANSFER, make_connection, make_leg, only_occurrence
from conftest import CHAT, STRANGER, TOKEN, FakeTelegram, error
from tcc_app import bot as bot_mod
from tcc_app.bot import Bot, ShellState, load_state, save_state
from tcc_app.commands import render_status, status_buttons
from tcc_app.telegram import TelegramApi
from test_runtime import PROFILE, Rig, at

from train_commute_companion.lifecycle import Commitment, DirectionState, LegState, LockSource
from train_commute_companion.location import LocationEvent

ONE_PROFILE = PROFILE.split('  - id: "p2"')[0].replace("NAME", "office_a")
OUT, RET = "p1|outbound|2026-10-27", "p1|return|2026-10-27"
OTHER_TOKEN = "456:TEST"


@dataclass
class App:
    rig: Rig
    tg: FakeTelegram
    path: Path
    bot: Bot

    @property
    def rt(self):
        return self.rig.rt

    def new_bot(self, chat: int | None = CHAT, token: str = TOKEN) -> Bot:
        return Bot(TelegramApi(token, self.tg.client()), self.rig.rt, chat, self.path)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.tg.sent]

    def msg(self, text: str, uid: int = 1, chat: int = CHAT, sent: datetime | None = None,
            kind: str = "message") -> dict[str, Any]:  # fmt: skip
        when = sent or self.rig.clock.now()
        body = {
            "message_id": uid,
            "date": int(when.timestamp()),
            "chat": {"id": chat},
            "text": text,
        }
        return {"update_id": uid, kind: body}


def tap(data: str, uid: int = 1, chat: int = CHAT) -> dict[str, Any]:
    message = {"message_id": 1, "date": 0, "chat": {"id": chat}}
    return {"update_id": uid, "callback_query": {"id": f"q{uid}", "data": data, "message": message}}


@pytest.fixture
async def app(tmp_path: Path, tg: FakeTelegram) -> AsyncIterator[App]:
    rig = Rig(tmp_path)
    rig.write(text=ONE_PROFILE)
    rig.provider.world = list(ONE_TRANSFER)
    await rig.rt.start()
    await rig.rt.poll_once()
    path = tmp_path / "telegram.json"
    a = App(rig, tg, path, Bot(TelegramApi(TOKEN, tg.client()), rig.rt, CHAT, path))
    yield a
    await a.bot.close()


async def until(cond: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not cond():
            await asyncio.sleep(0.001)


async def running(bot: Bot, cond: Callable[[], bool]) -> asyncio.Task[None]:
    task = asyncio.create_task(bot.run())
    await until(cond)
    return task


async def stop(task: asyncio.Task[None], bot: Bot) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await bot.close()


def hold(app: App) -> tuple[asyncio.Event, asyncio.Event]:
    """Holds the next NS request until `release` is set."""
    asked, release = asyncio.Event(), asyncio.Event()
    real = app.rig.provider.trips

    async def held(q):
        if not asked.is_set():
            asked.set()
            await release.wait()
        return await real(q)

    app.rig.provider.trips = held  # type: ignore[method-assign]
    return asked, release


def snapshot(app: App, key: str = OUT) -> tuple[Any, ...]:
    s = app.rig.occ(key)
    return s.tap_at, s.generation, s.lock, s.not_travelling


# --- commands ---


async def test_choose_locks_and_replies(app):
    await app.bot.handle(app.msg("/choose 08:44"))
    assert only_occurrence(app.rt).lock.legs[0].train.planned_departure == at("08:44")
    assert app.texts() == ["Locked the 08:44 for Office A."]


async def test_choose_refusal_reason_is_replied(app):
    before = only_occurrence(app.rt).lock
    await app.bot.handle(app.msg("/choose 08:13"))
    [reply] = app.texts()
    assert "no train at 08:13 from AAA" in reply and not reply.startswith("Locked")
    assert only_occurrence(app.rt).lock == before


async def test_choose_usage_without_departure(app):
    await app.bot.handle(app.msg("/choose outbound"))
    assert app.texts() == [bot_mod.USAGE_CHOOSE]


async def test_choose_retained_remainder_promotes_ridden_train(app):  # N3, required (11.3)
    rig = app.rig
    rig.clock.set("08:18")  # at AAA after the 08:12 left: it may still dwell with the user on it
    await app.rt.location("p1", LocationEvent("AAA", "report", at("08:18"), at("08:18")))
    rig.clock.set("08:20")
    await app.rt.poll_once()
    pre = only_occurrence(app.rt)
    assert pre.lock.legs[0].train.planned_departure == at("08:44")
    assert pre.retained[0].legs[0].train.planned_departure == at("08:12")
    ridden_state = pre.retained[0].legs[0].state
    assert ridden_state is LegState.MISSED
    await app.bot.handle(app.msg("/choose 08:12"))
    assert app.texts() == ["Locked the 08:12 for Office A."]
    s = only_occurrence(app.rt)
    assert s.lock.legs[0].train.planned_departure == at("08:12")
    assert s.lock.source is LockSource.CHOOSE
    assert s.lock.legs[0].state is ridden_state  # the ridden prefix keeps its progress
    assert s.direction_state is DirectionState.RIDING and s.retained == ()


async def test_leaving_uses_the_runtime_clock(app):
    app.rig.clock.set("08:00")
    await app.bot.handle(app.msg("/leaving"))
    s = only_occurrence(app.rt)
    assert s.lock.commitment is Commitment.COMMITTED and s.tap_at == app.rt.now()
    assert app.texts() == ["Marked as leaving now."]


async def test_skip_direction_and_whole_day(app):
    await app.bot.handle(app.msg("/skip outbound"))
    assert app.rig.occ(OUT).not_travelling and app.rig.occ(RET) is None
    # a bare /skip after the morning skip still reaches the return (not created yet)
    await app.bot.handle(app.msg("/skip", uid=2))
    assert app.rig.occ(RET).not_travelling
    assert app.texts() == [
        "Not travelling: alerts for that journey stop.",
        "Not travelling today: alerts for both directions stop.",
    ]


async def test_nothing_to_act_on_reply(app):
    app.rig.clock.set("12:00")
    await app.rt.watchdog_once()  # the morning journey ends
    await app.bot.handle(app.msg("/leaving"))
    assert app.texts() == ["Nothing to act on: no journey of Office A is being followed now."]


async def test_target_error_is_replied(app):
    await app.bot.handle(app.msg("/skip nobody"))
    assert app.texts() == ["No profile nobody. One of: office_a."]


async def test_buttons_answer_first_then_act(app):
    app.rig.clock.set("08:00")
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", 1))
    assert app.rig.occ(OUT).tap_at == app.rt.now()
    await app.bot.handle(tap("c|p1|outbound|2026-10-27|08:44|1236", 2))
    assert only_occurrence(app.rt).lock.legs[0].train.planned_departure == at("08:44")
    await app.bot.handle(tap("s|p1|outbound|2026-10-27", 3))
    assert app.rig.occ(OUT).not_travelling and app.rig.occ(RET) is None
    await app.bot.handle(tap("l|p1|nowhere|2026-10-27", 4))  # unparseable: answered, ignored
    assert app.tg.calls == ["answerCallbackQuery", "sendMessage"] * 3 + ["answerCallbackQuery"]
    assert app.tg.answered == ["q1", "q2", "q3", "q4"]
    assert app.texts() == [
        "Marked as leaving now.",
        "Locked the 08:44 for Office A.",
        "Not travelling: alerts for that journey stop.",
    ]


async def test_take_button_for_a_delayed_train_locks_it(app):
    late = make_connection([
        make_leg("AAA", "UT", "08:44", "09:08", number="1236", delay_dep=3),
        make_leg("UT", "RTD", "09:16", "09:50", number="5680"),
    ])  # fmt: skip
    app.rig.provider.world = [ONE_TRANSFER[0], late]
    app.rig.clock.set("07:52")
    await app.rt.poll_once()
    buttons = status_buttons(app.rt.view())
    assert buttons is not None
    take = buttons["inline_keyboard"][0][-1]
    assert take == {"text": "Take 08:47", "callback_data": "c|p1|outbound|2026-10-27|08:44|1236"}
    await app.bot.handle(tap(take["callback_data"]))
    assert only_occurrence(app.rt).lock.legs[0].train.planned_departure == at("08:44")
    assert app.texts() == ["Locked the 08:44 for Office A."]


async def test_other_chat_is_ignored_for_commands_buttons_and_location(app, caplog):
    caplog.set_level(logging.WARNING)
    before = snapshot(app)
    await app.bot.handle(app.msg("/skip day", chat=STRANGER))
    await app.bot.handle(tap("s|p1|outbound|2026-10-27", chat=STRANGER))
    location = app.msg("", chat=STRANGER)
    location["message"]["location"] = {"latitude": 52.1, "longitude": 5.1}
    await app.bot.handle(location)
    tg = app.tg
    assert (
        tg.sent == []
        and tg.answered == []
        and not any(o.not_travelling for o in app.rt.view().occurrences)
    )
    assert snapshot(app) == before and app.rig.occ(RET) is None
    assert "2000002" in caplog.text and "set TELEGRAM_CHAT_ID" in caplog.text


async def test_no_chat_id_ignores_every_chat(app, caplog):
    caplog.set_level(logging.WARNING)
    await app.new_bot(chat=None).handle(app.msg("/status"))
    assert app.tg.sent == [] and "1000001" in caplog.text


# --- delivery test (rule 16) ---


async def test_test_pair_and_confirmation_persist(app, tmp_path):
    await app.bot.handle(app.msg("/test"))
    loud, silent = app.tg.sent
    assert loud["text"] == bot_mod.LOUD_TEST and loud["disable_notification"] is False
    assert silent["text"] == bot_mod.SILENT_TEST and silent["disable_notification"] is True
    [[button]] = silent["reply_markup"]["inline_keyboard"]
    assert button["text"] == "I got both" and button["callback_data"].startswith("ok|")
    assert not app.bot.confirmed
    await app.bot.handle(tap(button["callback_data"], 2))
    assert app.texts()[-1] == "Delivery confirmed. Setup is complete."
    assert app.bot.confirmed and load_state(app.path).confirmed_for == "123:1000001"
    assert app.new_bot().confirmed


async def test_help_sends_the_test_pair_until_confirmed(app):
    await app.bot.handle(app.msg("/help"))
    help_text, loud, silent = app.texts()
    assert help_text.endswith("Data: NS") and "/choose" in help_text
    assert (loud, silent) == (bot_mod.LOUD_TEST, bot_mod.SILENT_TEST)
    await app.bot.handle(
        tap(app.tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], 2)
    )
    app.tg.sent.clear()
    await app.bot.handle(app.msg("/start", uid=3))
    assert app.texts() == [bot_mod.HELP]


async def test_confirmation_needs_both_sends_and_the_current_button(app, tmp_path):
    tg, sends = app.tg, []

    def nth_send_fails(n: int) -> Callable[[str], httpx.Response | None]:
        def fail(method: str) -> httpx.Response | None:
            if method != "sendMessage":
                return None
            sends.append(method)
            return error(500, "Internal Server Error") if len(sends) == n else None

        return fail

    tg.fail = nth_send_fails(1)  # the loud one fails: no silent message, no button
    await app.bot.handle(app.msg("/test"))
    assert tg.sent == [] and tg.calls == ["sendMessage"]
    sends.clear()
    tg.fail = nth_send_fails(2)  # the silent one fails: its button is never valid
    await app.bot.handle(app.msg("/test", uid=2))
    assert app.texts() == [bot_mod.LOUD_TEST]
    tg.fail = lambda m: None
    await app.bot.handle(app.msg("/test", uid=3))
    old = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(app.msg("/test", uid=4))
    current = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert old != current
    for data in ("ok|0000", old, "ok|", "ok|\u00e9"):
        await app.bot.handle(tap(data, 5))
        assert app.texts()[-1] == "That test is old; send /test again."
        assert not app.bot.confirmed and not app.path.exists()
    await app.bot.handle(tap(current, 6))
    assert app.bot.confirmed
    assert not app.new_bot(chat=STRANGER).confirmed  # another TELEGRAM_CHAT_ID
    assert not app.new_bot(token=OTHER_TOKEN).confirmed  # another bot
    await app.bot.handle(tap(current, 7))  # the nonce is spent
    assert app.texts()[-1] == "That test is old; send /test again."


# --- the update cursor (Review Focus 5) ---


async def test_restart_does_not_rerun_old_commands(app, tmp_path):
    tg = app.tg
    task = await running(app.bot, lambda: len(tg.polls) == 2)  # the discard, then the long poll
    tg.updates.put_nowait(app.msg("/skip day", uid=5))
    await until(lambda: len(tg.sent) == 1)
    await stop(task, app.bot)
    assert load_state(app.path) == ShellState(6, "123", None)
    again = app.new_bot()
    task = await running(again, lambda: len(tg.polls) == 3)
    await asyncio.sleep(0.01)
    await stop(task, again)
    assert tg.polls[2]["offset"] == 6 and tg.polls[2]["timeout"] == 30
    assert len(tg.sent) == 1


@pytest.mark.parametrize("state", ["missing", "corrupt", "another bot"])
async def test_untrusted_cursor_discards_the_backlog(app, tmp_path, state, caplog):
    caplog.set_level(logging.WARNING)
    if state == "corrupt":
        app.path.write_text("{not json")
    elif state == "another bot":
        save_state(app.path, ShellState(3, "456", "456:1000001"))
    bot = app.new_bot()
    tg, now = app.tg, app.rig.clock.now()
    for update in (
        app.msg("/leaving", uid=7, sent=now - timedelta(minutes=3)),
        tap("l|p1|outbound|2026-10-27", 8),
        tap("s|p1|outbound|2026-10-27", 9),
        tap("c|p1|outbound|2026-10-27|08:44|1236", 10),
    ):
        tg.updates.put_nowait(update)
    before = snapshot(app)
    task = await running(bot, lambda: len(tg.polls) == 2)
    await asyncio.sleep(0.01)
    await stop(task, bot)
    assert tg.polls[0] == {"offset": -1, "timeout": 0}
    assert tg.polls[1]["offset"] == 11
    assert snapshot(app) == before and tg.sent == [] and tg.answered == []
    assert "queued updates discarded" in caplog.text
    s = load_state(app.path)
    assert s is not None and (s.offset, s.bot_id) == (11, "123")
    if state == "another bot":
        assert s.confirmed_for == "456:1000001" and not bot.confirmed


def unwritable(monkeypatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(bot_mod.os, "replace", fail)
    monkeypatch.setattr(Path, "unlink", fail)


async def test_unwritable_state_keeps_the_bot_running(app, tmp_path, monkeypatch, caplog):
    save_state(app.path, ShellState(4, "123", None))  # an old file for this bot
    bot = app.new_bot()
    unwritable(monkeypatch)
    tg = app.tg
    tg.updates.put_nowait(app.msg("/leaving", uid=5))
    task = await running(bot, lambda: len(tg.sent) == 1)
    assert tg.polls[:2] == [
        {"offset": 4, "timeout": 30, "allowed_updates": bot_mod.ALLOWED},
        {"offset": 6, "timeout": 0},  # confirmed before the handler ran
    ]
    assert tg.calls.index("sendMessage") >= 2
    assert app.texts() == ["Marked as leaving now."] and not task.done()
    await stop(task, bot)
    after = snapshot(app)
    assert load_state(app.path) == ShellState(4, "123", None)  # still readable, still behind
    assert caplog.text.count("cannot save Telegram state") == 1
    again = app.new_bot()
    task = await running(again, lambda: len(tg.polls) == 4)
    await asyncio.sleep(0.01)
    await stop(task, again)
    assert snapshot(app) == after and len(tg.sent) == 1


async def test_unconfirmable_batch_is_not_dispatched(app, tmp_path, monkeypatch):
    save_state(app.path, ShellState(4, "123", None))
    bot = app.new_bot()
    unwritable(monkeypatch)
    monkeypatch.setattr(bot_mod, "RETRY_S", 0)
    tg = app.tg
    tg.fail = lambda m: (
        httpx.ConnectError("boom") if m == "getUpdates" and len(tg.polls) == 2 else None
    )
    tg.updates.put_nowait(app.msg("/leaving", uid=5))
    task = await running(bot, lambda: len(tg.sent) == 1)
    await asyncio.sleep(0.01)
    await stop(task, bot)
    assert [p["offset"] for p in tg.polls[:4]] == [4, 6, 4, 6]  # the failed confirm, then again
    assert tg.calls.index("sendMessage") >= 4
    assert app.texts() == ["Marked as leaving now."]


# --- what is read from an update ---


async def test_edited_command_is_ignored(app):
    before = snapshot(app)
    await app.bot.handle(app.msg("/skip day", kind="edited_message"))
    assert app.tg.sent == [] and snapshot(app) == before


async def test_command_with_bot_name_suffix(app):
    await app.bot.handle(app.msg("/status@SomeBot"))
    await app.bot.handle(app.msg("/status", uid=2))
    named, plain = app.tg.sent
    assert named == plain and named["text"] == render_status(app.rt.view(), False)
    assert named["text"].endswith("Data: NS") and "reply_markup" in named


async def test_old_command_across_the_autumn_repeat_hour(app, caplog):
    caplog.set_level(logging.INFO)
    app.rig.clock.t = datetime(2026, 10, 25, 2, 59, fold=1, tzinfo=AMS)  # second pass
    first_pass = datetime(2026, 10, 25, 2, 55, tzinfo=AMS)  # 64 min ago in UTC
    await app.bot.handle(app.msg("/status", sent=first_pass))
    assert app.tg.sent == [] and "ignored a Telegram command sent 64 min ago" in caplog.text
    await app.bot.handle(
        app.msg("/status", uid=2, sent=datetime(2026, 10, 25, 2, 55, fold=1, tzinfo=AMS))
    )
    assert len(app.tg.sent) == 1


async def test_slow_choose_does_not_block_status(app):
    save_state(app.path, ShellState(0, "123", None))
    bot = app.new_bot()
    asked, release = hold(app)
    app.tg.updates.put_nowait(app.msg("/choose 08:44", uid=1))
    app.tg.updates.put_nowait(app.msg("/status", uid=2))
    task = await running(bot, lambda: asked.is_set() and len(app.tg.sent) == 1)
    assert app.texts()[0].endswith("Data: NS")
    release.set()
    await until(lambda: len(app.tg.sent) == 2)
    assert app.texts()[1] == "Locked the 08:44 for Office A."
    await stop(task, bot)


async def test_choose_timeout_replies_and_the_core_call_finishes(app, monkeypatch):
    monkeypatch.setattr(bot_mod, "COMMAND_TIMEOUT_S", 0.05)
    asked, release = hold(app)
    before = only_occurrence(app.rt).lock
    await app.bot.handle(app.msg("/choose 08:44"))
    assert asked.is_set() and app.texts() == ["NS did not answer in time; try again."]
    assert only_occurrence(app.rt).lock == before
    release.set()
    await until(lambda: only_occurrence(app.rt).lock.legs[0].train.planned_departure == at("08:44"))
    assert len(app.tg.sent) == 1

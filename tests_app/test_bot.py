import asyncio
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import runtime_seam as seam
from app_rig import (
    CHAT,
    ONE_PROFILE,
    REPLAYS,
    SECRET,
    STRANGER,
    TOKEN,
    URL,
    URL2,
    FakeTelegram,
    build_from_replay,
    error,
    gated_saves,
    msg,
)
from builders import AMS, ONE_TRANSFER, make_connection, make_leg, only_occurrence
from runtime_rig import Rig, at
from tcc_app import bot as bot_mod
from tcc_app.bot import Bot, ShellState, load_state, save_state
from tcc_app.commands import render_status, render_webhook_tests, status_buttons
from tcc_app.telegram import TelegramApi
from webhook_receiver import Receiver

from train_commute_companion.history import JourneyHistory
from train_commute_companion.location import LocationEvent
from train_commute_companion.messages import RefusalKind, Refused
from train_commute_companion.model import Direction, DirectionState, LegState, OccurrenceKey, okey
from train_commute_companion.state import Commitment, LockSource
from train_commute_companion.webhook import WebhookTestResult

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


def tap(data: str, uid: int = 1, chat: int = CHAT, markup: Any = None) -> dict[str, Any]:
    message = {"message_id": 1, "date": 0, "chat": {"id": chat}}
    if markup is not None:
        message["reply_markup"] = markup
    return {"update_id": uid, "callback_query": {"id": f"q{uid}", "data": data, "message": message}}


async def test_stats_and_clear_confirmation_stay_in_the_bound_chat(app):
    save_state(app.path, ShellState(bot_id="123", confirmed_for=f"123:{CHAT}"))
    app.bot = app.new_bot()
    day = app.rt.now().date()
    key = OccurrenceKey("p1", Direction.OUTBOUND, day)
    row = JourneyHistory(
        key, at("08:12", day), at("09:20", day), at("09:28", day), False, False, False
    )
    seam.seed_state(app.rt, commute_history={okey(key): row})
    seam.save_state(app.rt)
    await app.bot.handle(app.msg("/stats year", uid=21))
    assert "Last 365 days" in app.texts()[-1]
    assert "Average delay 8 minutes" in app.texts()[-1]

    await app.bot.handle(app.msg("/clearstats", uid=22))
    assert "Delete all 1 local commute statistics rows" in app.texts()[-1]
    markup = app.tg.sent[-1]["reply_markup"]
    data = markup["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(tap(data, uid=23, chat=STRANGER))
    assert app.rt.state.commute_history

    await app.bot.handle(tap(data, uid=24, markup=markup))
    assert not app.rt.state.commute_history
    assert "Local commute statistics and their queued notes were deleted" in app.texts()[-1]


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


async def test_quick_trip_uses_confirmed_saved_pair_in_the_bound_private_chat(
    tmp_path, tg, monkeypatch
):
    from tcc_app.travel_actions import TravelActions
    from tcc_app.travel_preferences import TravelPreferences

    from train_commute_companion.model import Stations

    rig = Rig(tmp_path)
    await rig.rt.start()
    tick = 0
    monkeypatch.setattr("train_commute_companion.runtime.monotonic", lambda: tick)
    rig.rt.known_stations = Stations({"AAA": ("Alpha Central",), "RTD": ("Rotterdam Centraal",)})
    rig.provider.world = [
        make_connection([make_leg("AAA", "RTD", "08:25", "09:05")]),
        make_connection([make_leg("RTD", "AAA", "08:30", "09:10", number="2345")]),
    ]
    preferences = TravelPreferences(tmp_path / "travel-preferences.json", rig.rt.now)
    actions = TravelActions(rig.rt, {}, tmp_path / "agents.json", preferences)
    path = tmp_path / "telegram.json"
    a = App(
        rig,
        tg,
        path,
        Bot(TelegramApi(TOKEN, tg.client()), rig.rt, CHAT, path, travel_actions=actions),
    )
    change = {
        "action": "save",
        "preference": {
            "name": "Office run",
            "origin": "Alpha Central",
            "destination": "Rotterdam Centraal",
            "arrival_by": "08:00",
        },
    }
    review = preferences.preview(change)

    async def send(text, chat=CHAT, kind="private"):
        update = a.msg(text, chat=chat)
        update["message"]["chat"]["type"] = kind
        await a.bot.handle(update)

    calls = list(rig.provider.calls)
    try:
        await send("/trip Alpha Central -> Rotterdam Centraal", chat=STRANGER)
        await send("/trip AAA RTD", kind="group")
        assert not tg.sent and rig.provider.calls == calls
        await send("/trip saved Office run")
        assert "No saved travel preference" in a.texts()[-1]
        await send("/trip reverse Office run")
        assert "No saved travel preference" in a.texts()[-1]
        assert rig.provider.calls == calls
        assert preferences.save(change, review["review_token"], "confirm")["status"] == "saved"
        await send("/trip saved office RUN")
        assert "Alpha Central to Rotterdam Centraal" in a.texts()[-1]
        assert "08:25" in a.texts()[-1]
        assert len(rig.provider.calls) == len(calls) + 1
        assert rig.provider.calls[-1].at == rig.rt.now()
        await send("/trip Alpha Central -> Rotterdam Centraal")
        assert "30 seconds" in a.texts()[-1]
        tick += 30
        await send("/trip Alpha Central -> Rotterdam Centraal")
        assert "Alpha Central to Rotterdam Centraal" in a.texts()[-1]
        assert len(rig.provider.calls) == len(calls) + 2
        await send("/trip reverse Office run")
        assert "30 seconds" in a.texts()[-1]
        tick += 30
        await send("/trip reverse Office run")
        assert "Rotterdam Centraal to Alpha Central" in a.texts()[-1]
        assert "08:30" in a.texts()[-1]
        assert len(rig.provider.calls) == len(calls) + 3
        query = rig.provider.calls[-1]
        assert (query.origin, query.destination) == ("RTD", "AAA")
        assert preferences.saved_route("Office run") == ("Alpha Central", "Rotterdam Centraal")
    finally:
        await a.bot.close()


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


async def test_choose_retained_remainder_promotes_ridden_train(tmp_path, tg):  # N3 (11.3)
    # left home at 07:20, so the 07:42 was locked; the errand timeout at 08:05 auto-locked the
    # 08:12 and kept the departed 07:42 a retained remainder (7.12 rule 7)
    early = make_connection(
        [
            make_leg("AAA", "UT", "07:42", "08:10", number="1100"),
            make_leg("UT", "RTD", "08:20", "08:54", number="2200"),
        ]
    )
    rig = Rig(tmp_path, "07:20")
    rig.write(text=ONE_PROFILE)
    rig.provider.world = [early, *ONE_TRANSFER]
    await rig.rt.start()
    await rig.rt.poll_once()
    await rig.rt.report_location("p1", LocationEvent("home", "leave", at("07:20")))
    rig.clock.set("08:06")
    await rig.rt.poll_once()
    path = tmp_path / "telegram.json"
    app = App(rig, tg, path, Bot(TelegramApi(TOKEN, tg.client()), rig.rt, CHAT, path))
    try:
        pre = only_occurrence(app.rt)
        assert pre.lock.legs[0].train.planned_departure == at("08:12")
        assert pre.retained[0].legs[0].train.planned_departure == at("07:42")
        ridden_state = pre.retained[0].legs[0].state
        assert ridden_state is LegState.RIDING
        await app.bot.handle(app.msg("/choose 07:42"))
        assert app.texts() == ["Locked the 07:42 for Office A."]
        s = only_occurrence(app.rt)
        assert s.lock.legs[0].train.planned_departure == at("07:42")
        assert s.lock.source is LockSource.CHOOSE
        assert s.lock.legs[0].state is ridden_state  # the ridden prefix keeps its progress
        assert s.direction_state is DirectionState.RIDING and s.retained == ()
    finally:
        await app.bot.close()


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
    markup = status_buttons(app.rt.view())
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", 1, markup=markup))
    assert app.rig.occ(OUT).tap_at == app.rt.now()
    await app.bot.handle(tap("c|p1|outbound|2026-10-27|08:44|1236", 2, markup=markup))
    assert only_occurrence(app.rt).lock.legs[0].train.planned_departure == at("08:44")
    await app.bot.handle(tap("s|p1|outbound|2026-10-27", 3, markup=markup))
    assert app.rig.occ(OUT).not_travelling and app.rig.occ(RET) is None
    await app.bot.handle(tap("l|p1|nowhere|2026-10-27", 4, markup=markup))  # still answered
    used = ["answerCallbackQuery", "sendMessage", "editMessageReplyMarkup"]
    assert app.tg.calls == used * 3 + ["answerCallbackQuery", "sendMessage"]
    assert app.tg.answered == ["q1", "q2", "q3", "q4"]
    assert app.texts() == [
        "I'm leaving: Marked as leaving now.",
        "Take 08:44: Locked the 08:44 for Office A.",
        "Not travelling: alerts for that journey stop.",
        "I'm leaving: That button is no longer valid.",
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
    await app.bot.handle(tap(take["callback_data"], markup=buttons))
    assert only_occurrence(app.rt).lock.legs[0].train.planned_departure == at("08:44")
    assert app.texts() == ["Take 08:47: Locked the 08:44 for Office A."]


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


async def test_a_group_is_not_told_to_become_the_chat_id(app, caplog):
    caplog.set_level(logging.WARNING)
    await app.new_bot(chat=None).handle(app.msg("/status", chat=-1001000001))
    assert app.tg.sent == [] and "-1001000001" in caplog.text
    assert "set TELEGRAM_CHAT_ID" not in caplog.text and "not a private chat" in caplog.text


# --- every tap names its button; a used button goes, a one-time choice takes its keyboard ---

CLEARED = {"chat_id": CHAT, "message_id": 1, "reply_markup": {"inline_keyboard": []}}


def without(markup: dict[str, Any], data: str) -> dict[str, Any]:
    """The edit that removes only the button with `data`, dropping its row once empty."""
    rows = [[b for b in row if b["callback_data"] != data] for row in markup["inline_keyboard"]]
    return {
        "chat_id": CHAT,
        "message_id": 1,
        "reply_markup": {"inline_keyboard": [r for r in rows if r]},
    }


async def test_used_alert_button_goes_and_its_sibling_stays(app):
    app.rig.clock.set("08:00")
    leaving, skip = (f"{tag}|p1|outbound|2026-10-27" for tag in ("l", "s"))
    markup = {"inline_keyboard": [[{"text": "I'm leaving", "callback_data": leaving},
                                   {"text": "Not travelling", "callback_data": skip}]]}  # fmt: skip
    await app.bot.handle(tap(leaving, markup=markup))
    assert app.rig.occ(OUT).tap_at == app.rt.now()
    assert app.texts() == ["I'm leaving: Marked as leaving now."]
    kept = {"inline_keyboard": [[{"text": "Not travelling", "callback_data": skip}]]}
    assert app.tg.edits == [{"chat_id": CHAT, "message_id": 1, "reply_markup": kept}]
    await app.bot.handle(tap(skip, 2, markup=kept))  # the plan changed: the sibling still works
    assert app.rig.occ(OUT).not_travelling and app.tg.edits[-1] == CLEARED


async def test_concurrent_sibling_taps_cannot_restore_used_buttons(app, monkeypatch):
    app.rig.clock.set("08:00")
    markup = status_buttons(app.rt.view())
    leaving, skip = (f"{tag}|p1|outbound|2026-10-27" for tag in ("l", "s"))
    entered, release = asyncio.Event(), asyncio.Event()
    handle_request = app.tg.handler

    async def gated(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/editMessageReplyMarkup") and not entered.is_set():
            entered.set()
            await release.wait()
        return await handle_request(request)

    monkeypatch.setattr(app.tg, "handler", gated)
    await app.bot.close()
    app.bot = app.new_bot()
    first = asyncio.create_task(app.bot.handle(tap(leaving, markup=markup)))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(app.bot.handle(tap(skip, 2, markup=markup)))
    try:
        await until(lambda: app.rig.occ(OUT).not_travelling)
    finally:
        release.set()
        await asyncio.gather(first, second)
    final = app.tg.edits[-1]["reply_markup"]["inline_keyboard"]
    assert all(b["callback_data"] not in {leaving, skip} for row in final for b in row)


async def test_failed_outcome_reply_keeps_buttons_after_action(app):
    app.rig.clock.set("08:00")
    app.tg.fail = lambda method: error(400, "send failed") if method == "sendMessage" else None
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", markup=status_buttons(app.rt.view())))
    assert app.rig.occ(OUT).tap_at == app.rt.now()
    assert app.tg.sent == [] and "editMessageReplyMarkup" not in app.tg.calls


@pytest.mark.parametrize("tag", ["l", "s", "c"])
async def test_used_status_button_goes_and_every_other_stays(app, tag):
    app.rig.clock.set("08:00")
    markup = status_buttons(app.rt.view())
    assert markup is not None
    [row] = markup["inline_keyboard"]
    assert [b["callback_data"].split("|")[0] for b in row] == ["l", "s", "c"]
    data = next(b["callback_data"] for b in row if b["callback_data"].startswith(tag + "|"))
    await app.bot.handle(tap(data, markup=markup))
    assert app.tg.edits == [without(markup, data)]  # one row of three keeps the other two


async def test_a_used_button_missing_from_its_keyboard_keeps_the_keyboard(app, caplog):
    caplog.set_level(logging.INFO)
    app.rig.clock.set("08:00")
    other = {"inline_keyboard": [[{"text": "Other", "callback_data": "s|p1|return|2026-10-27"}]]}
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", markup=other))
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", 2))  # no keyboard to rebuild from
    assert app.texts() == ["I'm leaving: Marked as leaving now."] * 2
    assert (
        "editMessageReplyMarkup" not in app.tg.calls
        and "not on its Telegram message" in caplog.text
    )


async def test_refused_button_keeps_its_keyboard(app):
    markup = status_buttons(app.rt.view())
    await app.bot.handle(tap("l|nobody|outbound|2026-10-27", markup=markup))
    await app.bot.handle(tap("l|p1|nowhere|2026-10-27", 2, markup=markup))
    app.rt.choose_at = lambda *args: _refusal("no such train")
    take = markup["inline_keyboard"][0][-1]
    await app.bot.handle(tap(take["callback_data"], 3, markup=markup))
    [refused, invalid, refused_take] = app.texts()
    assert refused.startswith("I'm leaving: Nothing to act on: ")
    assert invalid == "I'm leaving: That button is no longer valid."
    assert refused_take == take["text"] + ": no such train" and take["text"].startswith("Take ")
    assert app.tg.edits == [] and "editMessageReplyMarkup" not in app.tg.calls


@pytest.mark.parametrize(
    "failure",
    [error(400, "Bad Request: message is not modified"), httpx.ConnectError("offline")],
)
async def test_a_failed_keyboard_removal_is_only_logged(app, caplog, failure):
    caplog.set_level(logging.INFO)
    app.rig.clock.set("08:00")
    app.tg.fail = lambda method: failure if method == "editMessageReplyMarkup" else None
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", markup=status_buttons(app.rt.view())))
    assert app.rig.occ(OUT).tap_at == app.rt.now()
    assert app.texts() == ["I'm leaving: Marked as leaving now."]
    assert app.tg.calls[-1] == "editMessageReplyMarkup" and app.tg.edits == []
    assert "keyboard removal failed" in caplog.text


async def test_a_crashed_button_is_named_and_keeps_its_keyboard(app, caplog):
    async def broken(*args):
        raise RuntimeError("boom")

    app.rt.leaving = broken
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", markup=status_buttons(app.rt.view())))
    assert app.texts() == ["I'm leaving: That failed; see the app log."]
    assert app.tg.edits == [] and "Telegram button failed" in caplog.text


async def test_a_tap_without_its_message_still_gets_a_named_reply(app):
    take = "c|p1|outbound|2026-10-27|08:44|1236"
    await app.bot._button({"id": "q1", "data": take})  # no message at all
    cb = {"update_id": 2, "callback_query": {"id": "q2", "data": "s|p1|outbound|2026-10-27",
                                            "message": {"chat": {"id": CHAT}}}}  # fmt: skip
    await app.bot.handle(cb)  # no message id and no keyboard: the fallback label, no edit
    assert app.texts() == [
        "Take train: Locked the 08:44 for Office A.",
        "Not travelling: alerts for that journey stop.",
    ]
    assert "editMessageReplyMarkup" not in app.tg.calls


async def _refusal(text: str) -> Refused:
    return Refused(text)


async def test_confirmed_delivery_button_after_a_restart(app):
    await app.bot.handle(app.msg("/test"))
    markup = app.tg.sent[-1]["reply_markup"]
    data = markup["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(tap(data, 2))
    await app.bot.close()  # the proof lives in memory only; the confirmation is saved
    app.bot = app.new_bot()
    await app.bot.handle(tap(data, 3, markup=markup))
    assert app.texts()[-1] == "I got both: Delivery is already confirmed."
    assert app.tg.edits == [CLEARED]  # the repeat changes nothing, so its keyboard stays


async def test_an_unsaved_delivery_proof_keeps_its_button_for_a_retry(app, monkeypatch):
    await app.bot.handle(app.msg("/test"))
    markup = app.tg.sent[-1]["reply_markup"]
    data = markup["inline_keyboard"][0][0]["callback_data"]

    def full(path: Path, s: ShellState) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(bot_mod, "save_state", full)
    await app.bot.handle(tap(data, 2, markup=markup))
    assert app.texts()[-1] == (
        "I got both: Delivery proof was not saved. Tap the confirmation again to retry."
    )
    assert not app.bot.confirmed and app.tg.edits == []
    monkeypatch.setattr(bot_mod, "save_state", save_state)
    await app.bot.handle(tap(data, 3, markup=markup))
    assert app.texts()[-1] == "I got both: Delivery confirmed. Setup is complete."
    assert app.bot.confirmed and app.tg.edits == [CLEARED]


async def test_second_tap_on_a_confirmed_delivery_button(app):
    await app.bot.handle(app.msg("/test"))
    markup = app.tg.sent[-1]["reply_markup"]
    data = markup["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(tap("ok|0000", 2, markup=markup))  # an old test: the keyboard stays
    assert app.tg.edits == []
    await app.bot.handle(tap(data, 3, markup=markup))
    await app.bot.handle(tap(data, 4, markup=markup))  # a no-op: the keyboard stays
    assert app.texts()[2:] == [
        "I got both: That test is old; send /test again.",
        "I got both: Delivery confirmed. Setup is complete.",
        "I got both: Delivery is already confirmed.",
    ]
    assert app.bot.confirmed and app.tg.edits == [CLEARED]


async def delivery_test(app: App, uid: int) -> tuple[str, dict[str, Any]]:
    await app.bot.handle(app.msg("/test", uid))
    markup = app.tg.sent[-1]["reply_markup"]
    return markup["inline_keyboard"][0][0]["callback_data"], markup


async def restart(app: App) -> None:
    await app.bot.close()
    app.bot = app.new_bot()


async def test_the_accepted_delivery_test_is_saved_and_others_stay_old(app):
    a, a_keys = await delivery_test(app, 1)
    b, b_keys = await delivery_test(app, 2)
    await app.bot.handle(tap(b, 3, markup=b_keys))
    assert app.bot.confirmed and load_state(app.path).accepted_nonce == b.split("|")[1]
    old, again = (
        "I got both: That test is old; send /test again.",
        "I got both: Delivery is already confirmed.",
    )
    for uid in (4, 6):  # before and after a restart
        edits = len(app.tg.edits)
        await app.bot.handle(tap(a, uid, markup=a_keys))  # superseded: old, its keyboard stays
        assert app.texts()[-1] == old and len(app.tg.edits) == edits
        await app.bot.handle(tap(b, uid + 1, markup=b_keys))
        assert app.texts()[-1] == again and len(app.tg.edits) == edits
        await restart(app)
    await app.bot._commit_and_dispatch(
        [app.msg("/help", 20, kind="edited_message")]
    )  # a cursor save
    assert load_state(app.path) == ShellState(21, None, "123:1000001", b.split("|")[1])
    c, c_keys = await delivery_test(app, 21)  # sent, never tapped
    await app.bot.handle(tap(b, 22, markup=b_keys))
    assert app.texts()[-1] == again
    await restart(app)
    edits = len(app.tg.edits)
    await app.bot.handle(tap(c, 23, markup=c_keys))  # unconfirmed and forgotten by the restart
    await app.bot.handle(tap(b, 24, markup=b_keys))
    assert app.texts()[-2:] == [old, again] and len(app.tg.edits) == edits
    d, d_keys = await delivery_test(app, 25)
    await app.bot.handle(tap(d, 26, markup=d_keys))  # a newer proof supersedes the saved one
    assert app.texts()[-1] == "I got both: Delivery confirmed. Setup is complete."
    await restart(app)
    await app.bot.handle(tap(b, 27, markup=b_keys))
    await app.bot.handle(tap(d, 28, markup=d_keys))
    assert app.texts()[-2:] == [old, again]


async def test_an_older_state_file_without_the_accepted_test_still_loads(app):
    app.path.write_text('{"offset": 4, "bot_id": "123", "confirmed_for": "123:1000001"}')
    app.bot = app.new_bot()
    assert app.bot.confirmed and app.bot.state.accepted_nonce is None
    await app.bot.handle(tap("ok|0a1b2c3d", 1, markup={"inline_keyboard": [[]]}))
    assert app.texts() == ["I got both: That test is old; send /test again."] and app.tg.edits == []
    data, markup = await delivery_test(app, 2)
    await app.bot.handle(tap(data, 3, markup=markup))
    assert app.texts()[-1] == "I got both: Delivery confirmed. Setup is complete."
    assert load_state(app.path) == ShellState(4, "123", "123:1000001", data.split("|")[1])


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
    assert app.texts()[-1] == "I got both: Delivery confirmed. Setup is complete."
    assert app.bot.confirmed and load_state(app.path).confirmed_for == "123:1000001"
    assert app.new_bot().confirmed


async def test_test_without_webhooks_is_as_today(app):
    await app.bot.handle(app.msg("/test"))
    assert app.texts() == [bot_mod.LOUD_TEST, bot_mod.SILENT_TEST]  # no webhook reply


async def test_older_delivery_test_cannot_replace_newer_proof(app, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    send = app.bot._send
    first = True

    async def delayed(text, **params):
        nonlocal first
        if text == bot_mod.LOUD_TEST and first:
            first = False
            started.set()
            await release.wait()
        await send(text, **params)

    monkeypatch.setattr(app.bot, "_send", delayed)
    older = asyncio.create_task(app.bot._test_pair())
    await started.wait()
    await app.bot._test_pair()
    newer_button = app.tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    release.set()
    await older
    older_button = app.tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(tap(older_button))
    assert not app.bot.confirmed
    await app.bot.handle(tap(newer_button, uid=2))
    assert app.bot.confirmed


async def test_delivery_proof_waits_for_storage_and_can_retry(app, monkeypatch):
    await app.bot._test_pair()
    button = app.tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    save = bot_mod.save_state

    def failed_save(path, state):
        raise OSError("read-only volume")

    monkeypatch.setattr(bot_mod, "save_state", failed_save)
    await app.bot.handle(tap(button))
    assert not app.bot.confirmed
    assert "Setup is complete" not in app.texts()[-1]
    monkeypatch.setattr(bot_mod, "save_state", save)
    await app.bot.handle(tap(button, uid=2))
    assert app.bot.confirmed
    assert load_state(app.path).confirmed_for == "123:1000001"


async def test_cursor_dispatches_unique_ordered_updates_without_rewinding(app):
    app.bot.state.offset = 4
    updates = [
        app.msg("/status", uid=7),
        app.msg("/status", uid=5),
        app.msg("/status", uid=5),
        app.msg("/skip day", uid=3),
        app.msg("/skip day", uid=True),
        app.msg("/skip day", uid=-1),
    ]
    before = snapshot(app)
    await app.bot._commit_and_dispatch(updates)
    await asyncio.gather(*app.bot._tasks)
    assert app.bot.state.offset == 8
    assert load_state(app.path).offset == 8
    assert len(app.tg.sent) == 2
    assert snapshot(app) == before


async def test_closed_bot_rejects_new_actions(app):
    before = snapshot(app)
    await app.bot.close()
    await app.bot.handle(app.msg("/skip day"))
    assert snapshot(app) == before
    assert not app.tg.sent


async def test_callback_waiting_for_ack_cannot_act_after_bot_closes(app, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    call = app.bot._api.call
    before = snapshot(app)

    async def delayed(method, params, timeout=20):
        if method == "answerCallbackQuery":
            entered.set()
            await release.wait()
        return await call(method, params, timeout)

    monkeypatch.setattr(app.bot._api, "call", delayed)
    callback = asyncio.create_task(app.bot.handle(tap("s|p1|outbound|2026-10-27")))
    await entered.wait()
    await app.bot.close()
    release.set()
    await callback
    assert snapshot(app) == before and app.tg.sent == []  # closed: no reply either


async def test_future_command_cannot_change_current_journey(app):
    before = snapshot(app)
    await app.bot.handle(app.msg("/skip day", sent=app.rt.now() + timedelta(days=1)))
    assert snapshot(app) == before
    assert not app.tg.sent


def test_negative_saved_cursor_is_not_trusted(tmp_path):
    path = tmp_path / "telegram.json"
    save_state(path, ShellState(-1, "123", None))
    assert load_state(path) is None


async def test_telegram_test_reports_each_webhook(tmp_path, tg):  # Review Focus 11
    app, rp = build_from_replay(tmp_path, tg, REPLAYS / "direct.yaml",
                                webhook_urls=(URL, URL2), webhook_secret=SECRET)  # fmt: skip
    rx = Receiver(SECRET, lambda: rp.clock.now().timestamp())
    other: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hooks.example.invalid":
            return rx.handler(request)
        other.append(request.url.host)
        return httpx.Response(503)

    tg.other = route  # the runtime is not started: its first alert would post a real copy
    await app.bot.handle(msg(app, "/test"))
    loud, silent, reply = tg.sent
    assert (loud["text"], loud["disable_notification"]) == (bot_mod.LOUD_TEST, False)
    assert (silent["text"], silent["disable_notification"]) == (bot_mod.SILENT_TEST, True)
    [[button]] = silent["reply_markup"]["inline_keyboard"]
    assert reply["text"] == render_webhook_tests(
        [WebhookTestResult("hooks.example.invalid", 200, 200),
         WebhookTestResult("hooks2.example.invalid", 503, 503)]
    )  # fmt: skip
    assert [(p["severity"], p["test"]) for p in rx.posts] == [("loud", True), ("silent", True)]
    assert rx.bad == 0 and other == ["hooks2.example.invalid"] * 2
    assert not app.bot.confirmed  # the button is the proof
    assert app.webhooks is not None and not app.webhooks.failing
    await app.rt.webhooks_idle()
    assert (
        seam.owed_copies(app.rt) == {} and not (tmp_path / "tcc-state.json").exists()
    )  # never stored
    await app.bot.handle(tap(button["callback_data"], 2))
    assert tg.sent[-1]["text"] == "I got both: Delivery confirmed. Setup is complete."
    assert app.bot.confirmed
    await app.bot.close()


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


def nth_send_fails(n: int) -> Callable[[str], httpx.Response | None]:
    sends = 0

    def fail(method: str) -> httpx.Response | None:
        nonlocal sends
        if method != "sendMessage":
            return None
        sends += 1
        return error(500, "Internal Server Error") if sends == n else None

    return fail


async def test_confirmation_needs_both_sends_and_the_current_button(app, tmp_path):
    tg = app.tg
    tg.fail = nth_send_fails(1)  # the loud one fails: no silent message, no button
    await app.bot.handle(app.msg("/test"))
    assert tg.sent == [] and tg.calls == ["sendMessage"]
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
        assert app.texts()[-1] == "I got both: That test is old; send /test again."
        assert not app.bot.confirmed and not app.path.exists()
    await app.bot.handle(tap(current, 6))
    assert app.bot.confirmed
    assert not app.new_bot(chat=STRANGER).confirmed  # another TELEGRAM_CHAT_ID
    assert not app.new_bot(token=OTHER_TOKEN).confirmed  # another bot
    await app.bot.handle(tap(current, 7))  # a second tap on the accepted button
    assert app.texts()[-1] == "I got both: Delivery is already confirmed."
    assert app.bot.confirmed


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
        save_state(app.path, ShellState(3, "456", "456:1000001", "0a1b2c3d"))
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
        assert s.accepted_nonce == "0a1b2c3d"  # it names that bot's chat, so it travels with it


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


async def test_state_saves_run_off_the_loop(app, monkeypatch):
    save_state(app.path, ShellState(4, "123", None))  # rule 12: a slow volume never stalls polling
    bot = app.new_bot()
    tg, saves = app.tg, []

    def spy(path: Path, s: ShellState) -> None:
        time.sleep(0.02)  # a handler dispatched before the save returned would send meanwhile
        save_state(path, s)
        saves.append((threading.get_ident(), s.offset, len(tg.sent)))

    monkeypatch.setattr(bot_mod, "save_state", spy)
    tg.updates.put_nowait(app.msg("/leaving", uid=5))
    task = await running(bot, lambda: len(tg.sent) == 1)
    await stop(task, bot)
    [(thread, offset, sent)] = saves
    assert thread != threading.get_ident() and (offset, sent) == (6, 0)
    assert load_state(app.path) == ShellState(6, "123", None)


async def test_overlapping_state_saves_keep_the_newest(app, monkeypatch):
    await app.bot.handle(app.msg("/test"))
    [[button]] = app.tg.sent[-1]["reply_markup"]["inline_keyboard"]
    entered, gate, guard = threading.Event(), threading.Event(), threading.Lock()
    active, peak, released = [0], [0], []

    def spy(path: Path, s: ShellState) -> None:
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        if not entered.is_set():
            entered.set()
            released.append(gate.wait(2))
        save_state(path, s)
        with guard:
            active[0] -= 1

    monkeypatch.setattr(bot_mod, "save_state", spy)
    first = asyncio.create_task(
        app.bot._commit_and_dispatch([app.msg("/help", uid=7, kind="edited_message")])
    )
    await until(entered.is_set)
    tap_task = asyncio.create_task(app.bot.handle(tap(button["callback_data"], 8)))
    await until(lambda: len(app.bot.writes) == 2)
    assert not app.bot.confirmed
    await asyncio.sleep(0.01)  # the confirm's save waits for the first one
    gate.set()
    await asyncio.gather(first, tap_task)
    assert released == [True] and peak[0] == 1
    nonce = button["callback_data"].split("|")[1]
    assert load_state(app.path) == ShellState(8, None, "123:1000001", nonce)


async def test_a_cancelled_save_still_blocks_the_next_one(app, monkeypatch):
    entered, gate, written, peak = gated_saves(monkeypatch)
    app.bot.state.offset = 4
    first = asyncio.create_task(app.bot._save())
    await until(entered.is_set)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    app.bot.state.offset = 8
    second = asyncio.create_task(app.bot._save())
    await asyncio.sleep(0.02)  # the cancelled worker still holds the lock
    assert written == [] and not second.done()
    gate.set()
    assert await second
    assert written == [4, 8] and peak[0] == 1 and load_state(app.path).offset == 8


async def test_close_waits_for_a_running_save(app, monkeypatch):
    entered, gate, written, _ = gated_saves(monkeypatch)
    app.bot.state.offset = 4
    caller = asyncio.create_task(app.bot._save())
    await until(entered.is_set)
    caller.cancel()
    closing = asyncio.create_task(app.bot.close())
    await asyncio.sleep(0.02)
    assert not closing.done()
    gate.set()
    await closing
    assert written == [4] and load_state(app.path).offset == 4


async def test_taps_and_test_during_a_blocked_confirm_save(app, monkeypatch):
    await app.bot.handle(app.msg("/test"))
    [[old]] = app.tg.sent[-1]["reply_markup"]["inline_keyboard"]
    entered, gate, _, _ = gated_saves(monkeypatch)
    confirm = asyncio.create_task(app.bot.handle(tap(old["callback_data"], 2)))
    await until(entered.is_set)
    await app.bot.handle(tap(old["callback_data"], 3))  # a double tap while the save waits
    await app.bot.handle(app.msg("/test", uid=4))  # a fresh pair while the save waits
    [[new]] = app.tg.sent[-1]["reply_markup"]["inline_keyboard"]
    gate.set()
    await confirm
    await app.bot.handle(tap(new["callback_data"], 5))
    replies = [t for t in app.texts() if t.startswith("I got both")]
    assert replies == [
        "I got both: Delivery confirmation is being saved.",
        "I got both: Delivery confirmed. Setup is complete.",
        "I got both: Delivery confirmed. Setup is complete.",
    ]


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


async def test_a_failed_command_is_answered_without_its_text(app, monkeypatch, caplog):
    async def broken(*args):
        raise ValueError("inner detail")

    monkeypatch.setattr(app.rt, "choose_at", broken)
    await app.bot.handle(app.msg("/choose 08:44"))
    assert app.texts() == ["That failed; see the app log."]
    assert "Telegram command failed" in caplog.text


@pytest.mark.parametrize("command", ["/choose 08:44", "/leaving", "/skip"])
async def test_a_failed_view_is_not_read_as_a_target_refusal(app, monkeypatch, caplog, command):
    def broken():
        raise ValueError("inner detail")

    monkeypatch.setattr(app.rt, "view", broken)
    await app.bot.handle(app.msg(command))
    assert app.texts() == ["That failed; see the app log."]
    assert "Telegram command failed" in caplog.text


def test_shell_state_rejects_a_bool_offset_and_syncs_what_it_saves(tmp_path, monkeypatch):
    path = tmp_path / "telegram.json"
    path.write_text('{"offset": true}', encoding="utf-8")
    assert load_state(path) is None
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)
    save_state(path, ShellState(3))
    assert synced and load_state(path) == ShellState(3)


async def test_a_question_button_answers_and_a_stale_one_says_so(app):
    got: list[tuple[str, str]] = []

    async def answer(prompt_id: str, answer: str):
        got.append((prompt_id, answer))
        return None if answer == "yes" else Refused("this question has expired")

    app.rt.answer_prompt = answer
    await app.bot.handle(tap("q|0123456789|yes", 1))
    await app.bot.handle(tap("q|0123456789|no", 2))
    assert got == [("0123456789", "yes"), ("0123456789", "no")]
    assert app.texts() == ["Answer: Answer saved.", "Answer: this question has expired"]
    assert app.tg.edits == [
        {"chat_id": CHAT, "message_id": 1, "reply_markup": {"inline_keyboard": []}}
    ]


async def test_stations_lists_and_forget_removes_a_remembered_station(app):
    await app.bot.handle(app.msg("/stations", 1))
    seam.seed_state(app.rt, remembered_stations={"p1|return": ("VL",)})
    seam.save_state(app.rt)
    await app.bot.handle(app.msg("/stations", 2))
    await app.bot.handle(app.msg("/forget vl", 3))
    await app.bot.handle(app.msg("/forget vl", 4))
    assert app.texts() == [
        "No remembered boarding stations.",
        "Remembered boarding stations:\nOffice A return: VL",
        "Forgot VL.",
        "VL is not a remembered boarding station.",
    ]
    assert app.rt.state.remembered_stations == {}


async def test_forget_reports_a_failed_or_partial_removal(app):
    retry = Refused("The change was not saved. Try again.", RefusalKind.RETRY)
    gone = Refused("VL is not a remembered station of outbound trips")
    results = {Direction.OUTBOUND: None, Direction.RETURN: retry}

    async def forget(pid, direction, code):
        return results[direction]

    app.rt.forget_station = forget
    await app.bot.handle(app.msg("/forget vl", 1))
    results[Direction.OUTBOUND] = gone
    await app.bot.handle(app.msg("/forget vl", 2))
    assert app.texts() == [
        "Forgot VL for some trips, but: The change was not saved. Try again.",
        "The change was not saved. Try again.",
    ]

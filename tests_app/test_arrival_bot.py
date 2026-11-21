"""Telegram arrival records require an explicit reviewed date, offset and destination."""

import asyncio
from dataclasses import replace

import pytest
from app_rig import CHAT, STRANGER, TOKEN, FakeTelegram
from runtime_rig import PROFILE, Rig, at
from tcc_app import bot as bot_module
from tcc_app.bot import Bot
from tcc_app.telegram import TelegramApi, alert_buttons

from train_commute_companion.messages import Refused
from train_commute_companion.model import okey


@pytest.fixture
async def app(tmp_path):
    rig = Rig(tmp_path, now="08:05")
    rig.write(text=PROFILE[: PROFILE.index('  - id: "p2"')])
    await rig.rt.start()
    rig.clock.t = at("10:05")
    tg = FakeTelegram()
    bot = Bot(TelegramApi(TOKEN, tg.client()), rig.rt, CHAT, tmp_path / "telegram.json")
    bot.state.confirmed_for = f"123:{CHAT}"
    yield rig, tg, bot
    await bot.close()
    await tg.close()


def message(rig, text, chat=CHAT):
    return {"message": {"chat": {"id": chat}, "date": int(rig.rt.now().timestamp()), "text": text}}


def tap(data, chat=CHAT):
    return {
        "callback_query": {
            "id": "arrival-callback",
            "data": data,
            "message": {"chat": {"id": chat}},
        }
    }


def outbound(rig):
    return next(r for r in rig.rt.arrival_records() if r.key.direction.value == "outbound")


async def review(app, arrival="2026-10-27T10:00+01:00"):
    rig, tg, bot = app
    await bot.handle(message(rig, f"/arrived p1 outbound 2026-10-27 RTD {arrival}"))
    assert outbound(rig).arrived_at is None
    assert "actual arrival" in tg.sent[-1]["text"]
    return tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]


async def test_reviewed_arrival_and_duplicate_callback(app):
    rig, tg, bot = app
    data = await review(app)
    assert len(data.encode()) <= 64
    await bot.handle(tap(data))
    assert outbound(rig).arrived_at == at("10:00")
    assert "Arrival saved." in tg.sent[-1]["text"]
    assert "does not decide eligibility" in tg.sent[-1]["text"]
    rig.clock.jump(1)
    await bot.handle(tap(data))
    assert outbound(rig).revision == 1


@pytest.mark.parametrize("answer", ["yes", "other"])
async def test_prompt_callback_uses_bound_private_sender(app, answer):
    rig, tg, bot = app
    rig.path.write_text(rig.path.read_text() + "    alerts: {compensation_reminder: true}\n")
    assert await rig.rt.reload() is None
    rig.rt._arrival_recipient = lambda: bot.arrival_recipient
    original = outbound(rig)
    record, obligation = rig.rt._arrival_question(
        original, rig.rt.config.profiles[0], at("09:54"), rig.rt.now()
    )
    assert rig.rt._writer.commit_strict(
        replace(
            rig.rt.state,
            obligations=(*rig.rt.state.obligations, obligation),
            journey_records={**rig.rt.state.journey_records, okey(record.key): record},
        )
    )
    await rig.rt._deliver()
    assert outbound(rig).prompt.delivered
    buttons = alert_buttons(obligation.event)["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Yes", "Other time"]
    data = next(b["callback_data"] for b in buttons if b["callback_data"].endswith("|" + answer))
    assert len(data.encode()) <= 64

    def callback(sender, chat_type="private"):
        update = tap(data)
        update["callback_query"]["from"] = {"id": sender}
        update["callback_query"]["message"]["chat"]["type"] = chat_type
        return update

    for foreign in (callback(STRANGER), callback(CHAT, "group")):
        await bot.handle(foreign)
        assert not tg.sent and outbound(rig).arrived_at is None

    await bot.handle(callback(CHAT))
    if answer == "yes":
        assert outbound(rig).arrived_at == at("09:54")
        assert "Arrival saved." in tg.sent[-1]["text"]
    else:
        assert outbound(rig).arrived_at is None
        assert "/arrived p1 outbound 2026-10-27 RTD" in tg.sent[-1]["text"]


async def test_arrival_button_is_named_and_removed_once_saved(app):
    _, tg, bot = app
    data = await review(app)
    markup = tg.sent[-1]["reply_markup"]
    old = {"callback_query": {"id": "c1", "data": "av|" + "0" * 24,
                              "message": {"chat": {"id": CHAT}, "message_id": 4}}}  # fmt: skip
    await bot.handle(old)
    assert (
        tg.sent[-1]["text"] == "Confirm arrival: That arrival review is old. Enter /arrived again."
    )
    assert tg.edits == []
    used = {"chat": {"id": CHAT}, "message_id": 5, "reply_markup": markup}
    await bot.handle({"callback_query": {"id": "c2", "data": data, "message": used}})
    assert tg.sent[-1]["text"].startswith(
        "Confirm this destination and arrival time: Arrival saved."
    )
    assert tg.edits == [{"chat_id": CHAT, "message_id": 5, "reply_markup": {"inline_keyboard": []}}]


async def test_a_refused_arrival_keeps_its_keyboard(app, monkeypatch):
    rig, tg, bot = app
    data = await review(app)

    async def refuse(*args, **kwargs):
        return Refused("That journey changed.")

    monkeypatch.setattr(rig.rt, "confirm_arrival", refuse)
    refused = tap(data)
    refused["callback_query"]["message"]["message_id"] = 5
    await bot.handle(refused)
    assert tg.sent[-1]["text"] == "Confirm arrival: That journey changed."
    assert tg.edits == []


async def test_arrival_timeout_leaves_the_owned_action_running(app, monkeypatch):
    rig, tg, bot = app
    data = await review(app)
    entered, release = asyncio.Event(), asyncio.Event()
    confirm = rig.rt.confirm_arrival

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await confirm(*args, **kwargs)

    monkeypatch.setattr(rig.rt, "confirm_arrival", delayed)
    monkeypatch.setattr(bot_module, "COMMAND_TIMEOUT_S", 0.01)
    try:
        timed = tap(data)
        timed["callback_query"]["message"]["message_id"] = 5
        await asyncio.wait_for(bot.handle(timed), 1)
        assert entered.is_set() and outbound(rig).arrived_at is None
        assert tg.sent[-1]["text"] == "Confirm arrival: " + bot_module.TIMED_OUT
        assert "editMessageReplyMarkup" not in tg.calls and tg.edits == []
    finally:
        release.set()
    async with asyncio.timeout(1):
        while outbound(rig).arrived_at is None:
            await asyncio.sleep(0)
    assert outbound(rig).arrived_at == at("10:00")


async def test_cancelled_arrival_caller_does_not_cancel_its_delivery(app, monkeypatch):
    from train_commute_companion.model import EventKind

    rig, _tg, bot = app
    text = PROFILE[: PROFILE.index('  - id: "p2"')]
    rig.write(text=text + "    alerts: {compensation_reminder: true}\n")
    await rig.rt.reload()
    data = await review(app)
    entered, release, accepted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    send = rig.notifier.send

    async def delayed(event):
        if event.kind is EventKind.COMPENSATION_REMINDER:
            entered.set()
            await release.wait()
            accepted.set()
        return await send(event)

    monkeypatch.setattr(rig.notifier, "send", delayed)
    caller = asyncio.create_task(bot.handle(tap(data)))
    await asyncio.wait_for(entered.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.wait_for(accepted.wait(), 1)
    async with asyncio.timeout(1):
        while rig.rt.state.obligations:
            await asyncio.sleep(0)
    assert outbound(rig).arrived_at == at("10:00")
    await bot.handle(tap(data))
    reminders = [e for e in rig.notifier.attempts if e.kind is EventKind.COMPENSATION_REMINDER]
    assert len(reminders) == 1


async def test_detached_arrival_failure_is_observed_once(app, monkeypatch, caplog):
    rig, _tg, bot = app
    data = await review(app)
    release = asyncio.Event()

    async def failed(*args, **kwargs):
        await release.wait()
        raise RuntimeError("late arrival failure")

    monkeypatch.setattr(rig.rt, "confirm_arrival", failed)
    monkeypatch.setattr(bot_module, "COMMAND_TIMEOUT_S", 0.01)
    try:
        await asyncio.wait_for(bot.handle(tap(data)), 1)
    finally:
        release.set()
    async with asyncio.timeout(1):
        while not any(r.message == "detached command failed" for r in caplog.records):
            await asyncio.sleep(0)
    assert sum(r.message == "detached command failed" for r in caplog.records) == 1


async def test_no_implicit_time_wrong_destination_and_foreign_chat(app):
    rig, tg, bot = app
    for text in (
        "/arrived",
        "/arrived p1 outbound 2026-10-27 RTD 10:00",
        "/arrived p1 outbound 2026-10-27 RTD 2026-10-27T10:00",
    ):
        await bot.handle(message(rig, text))
        assert "Usage:" in tg.sent[-1]["text"]
    await bot.handle(message(rig, "/arrived p1 outbound 2026-10-27 UT 2026-10-27T10:00+01:00"))
    assert "No saved journey" in tg.sent[-1]["text"]
    data = await review(app)
    await bot.handle(tap(data, STRANGER))
    assert outbound(rig).arrived_at is None
    rig.clock.jump(11)
    await bot.handle(tap(data))
    assert "old" in tg.sent[-1]["text"]
    assert outbound(rig).arrived_at is None


async def test_clock_rollback_invalidates_a_review_even_when_the_arrival_is_not_future(app):
    rig, tg, bot = app
    data = await review(app)
    rig.clock.jump(-1)
    await bot.handle(tap(data))
    assert "old" in tg.sent[-1]["text"]
    assert outbound(rig).arrived_at is None


async def test_failed_write_requires_explicit_retry_and_old_review_refused(app):
    rig, tg, bot = app
    data = await review(app)
    rig.store.fail = True
    await bot.handle(tap(data))
    assert "not saved" in tg.sent[-1]["text"] and outbound(rig).arrived_at is None
    rig.store.fail = False
    await rig.restart()
    assert outbound(rig).arrived_at is None
    bot._rt = rig.rt
    await bot.handle(tap(data))
    assert outbound(rig).arrived_at == at("10:00")
    # A second review is bound to revision one, then another explicit correction wins.
    await bot.handle(message(rig, "/arrived p1 outbound 2026-10-27 RTD 2026-10-27T09:59+01:00"))
    old = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    record = outbound(rig)
    await rig.rt.confirm_arrival(
        record.key, "RTD", at("09:58"), expected_revision=1, action_id="other"
    )
    await bot.handle(tap(old))
    assert "changed" in tg.sent[-1]["text"]

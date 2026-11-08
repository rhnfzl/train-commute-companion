"""Telegram arrival records require an explicit reviewed date, offset and destination."""

import asyncio

import pytest
from app_rig import CHAT, STRANGER, TOKEN, FakeTelegram
from runtime_rig import PROFILE, Rig, at
from tcc_app import bot as bot_module
from tcc_app.bot import Bot
from tcc_app.telegram import TelegramApi


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
        await asyncio.wait_for(bot.handle(tap(data)), 1)
        assert entered.is_set() and outbound(rig).arrived_at is None
        assert "in time" in tg.sent[-1]["text"]
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

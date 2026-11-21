import asyncio
import json
import logging
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from app_rig import (
    CHAT,
    CODES,
    STRANGER,
    TickClock,
    error,
    free_port,
    gated_saves,
    make_app,
    ok,
    until,
)
from runtime_rig import StationsProvider, at
from tcc_app import bot as bot_mod
from tcc_app.bot import ShellState, load_state, save_state
from tcc_app.main import build, serve
from tcc_app.web import health_report
from test_bot import tap
from test_telegram import event

CODE = "deterministic-test-claim-code"
LINK = "https://t.me/tcc_test_bot?start=" + CODE


def start(app, code=CODE, chat=CHAT, kind="private", command="message"):
    return {
        "update_id": 1,
        command: {
            "message_id": 1,
            "date": int(app.rt.now().timestamp()),
            "chat": {"id": chat, "type": kind},
            "text": "/start " + code,
        },
    }


def rebuild(app, tg, **settings):
    return build(
        replace(app.settings, **settings),
        clock=app.rt._clock,
        provider=app.rt._provider,
        client=tg.client(),
    )


@pytest.fixture
async def unclaimed(tmp_path, tg, monkeypatch, caplog):
    monkeypatch.setattr(bot_mod.secrets, "token_urlsafe", lambda size: CODE)
    caplog.set_level(logging.INFO, logger="tcc_app.bot")
    tg.fail = lambda method: (
        ok(
            {
                "id": 123,
                "is_bot": True,
                "first_name": "TCC",
                "username": "tcc_test_bot",
            }
        )
        if method == "getMe"
        else None
    )
    app = make_app(tmp_path, tg, TickClock(at("07:50")), chat_id=None)
    await app.rt.start()
    yield app
    await app.bot.close()


async def test_unclaimed_run_prints_actual_username_link_and_local_qr(unclaimed, tg, caplog):
    task = asyncio.create_task(unclaimed.bot.run())
    try:
        await until(lambda: len(tg.polls) >= 2)
        assert LINK in caplog.text
        assert "█" in caplog.text or "▄" in caplog.text or "▀" in caplog.text
        assert tg.calls.index("getUpdates") < tg.calls.index("getMe")
        assert not unclaimed.bot.confirmed and unclaimed.notifier.destination_id is None
        assert CODE not in (unclaimed.settings.state_dir / "telegram.json").read_text()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_private_claim_persists_routes_and_still_needs_delivery_proof(unclaimed, tg):
    app = unclaimed
    await app.bot._setup_claim()
    await app.bot.handle(start(app))
    saved = load_state(app.settings.state_dir / "telegram.json")
    assert saved.claimed_for == "123:1000001"
    assert not app.bot.confirmed
    assert health_report(app.rt.view(), True, app.bot.confirmed)[0] == 503
    assert [p["disable_notification"] for p in tg.sent[-2:]] == [False, True]
    assert await app.notifier.send(event())
    assert tg.sent[-1]["chat_id"] == CHAT
    button = tg.sent[-2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(tap(button, chat=STRANGER))
    assert not app.bot.confirmed
    await app.bot.handle(tap(button))
    assert app.bot.confirmed
    assert health_report(app.rt.view(), True, app.bot.confirmed)[0] == 200


@pytest.mark.parametrize(
    "change",
    [
        {"code": "wrong"},
        {"code": CODE + "\ud800"},
        {"chat": -1001, "kind": "group"},
        {"chat": -1001, "kind": "channel"},
        {"kind": "group"},
        {"kind": None},
        {"chat": True},
        {"command": "edited_message"},
        {"code": CODE + " extra"},
    ],
)
async def test_invalid_claim_cannot_bind_or_send(unclaimed, tg, change):
    await unclaimed.bot._setup_claim()
    await unclaimed.bot.handle(start(unclaimed, **change))
    assert unclaimed.notifier.destination_id is None
    assert not unclaimed.bot.confirmed and not tg.sent
    assert not (unclaimed.settings.state_dir / "telegram.json").exists()


async def test_expired_and_old_future_messages_cannot_claim(unclaimed, tg):
    app = unclaimed
    await app.bot._setup_claim()
    for delta in (-601, 121):
        update = start(app)
        update["message"]["date"] += delta
        await app.bot.handle(update)
    app.rt._clock.t += timedelta(minutes=15)
    await app.bot.handle(start(app))
    assert not tg.sent and app.notifier.destination_id is None


async def test_claim_is_single_use_and_second_chat_commands_are_unauthorized(unclaimed, tg):
    app = unclaimed
    await app.bot._setup_claim()
    await asyncio.gather(app.bot.handle(start(app)), app.bot.handle(start(app, chat=STRANGER)))
    assert load_state(app.settings.state_dir / "telegram.json").claimed_for == "123:1000001"
    before = len(tg.sent)
    await app.bot.handle(start(app, chat=STRANGER))
    await app.bot.handle(start(app))
    assert len(tg.sent) == before
    foreign = start(app, chat=STRANGER)
    foreign["message"]["text"] = "/skip day"
    await app.bot.handle(foreign)
    assert len(tg.sent) == before
    assert not any(o.not_travelling for o in app.rt.view().occurrences)


async def test_restart_restores_binding_and_confirmation_without_getme(unclaimed, tg, caplog):
    app = unclaimed
    await app.bot._setup_claim()
    await app.bot.handle(start(app))
    button = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(tap(button))
    await app.bot.close()
    caplog.clear()
    again = rebuild(app, tg)
    await again.bot._setup_claim()
    assert again.bot.confirmed and again.notifier.destination_id == app.notifier.destination_id
    assert LINK not in caplog.text
    assert tg.calls.count("getMe") == 1
    await again.bot.close()


async def test_explicit_override_never_inherits_or_moves_saved_confirmation(unclaimed, tg):
    app = unclaimed
    await app.bot._setup_claim()
    await app.bot.handle(start(app))
    button = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await app.bot.handle(tap(button))
    override = rebuild(app, tg, chat_id=STRANGER)
    assert not override.bot.confirmed
    await override.bot.handle(tap(button, chat=STRANGER))
    assert not override.bot.confirmed
    await override.bot.handle(
        {
            "message": {
                "date": int(app.rt.now().timestamp()),
                "chat": {"id": STRANGER},
                "text": "/test",
            }
        }
    )
    button = tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await override.bot.handle(tap(button, chat=STRANGER))
    assert override.bot.confirmed
    restored = rebuild(app, tg)
    assert not restored.bot.confirmed
    assert restored.notifier.destination_id == app.notifier.destination_id
    await override.bot.close()
    await restored.bot.close()


async def test_failed_claim_save_keeps_code_available_and_routing_unconfigured(
    unclaimed,
    tg,
    monkeypatch,
    caplog,
):
    app = unclaimed
    await app.bot._setup_claim()
    caplog.clear()
    real = bot_mod.save_state

    def fail(*args):
        raise OSError(CODE)

    monkeypatch.setattr(bot_mod, "save_state", fail)
    await app.bot.handle(start(app))
    assert not tg.sent and app.notifier.destination_id is None
    assert CODE not in caplog.text
    monkeypatch.setattr(bot_mod, "save_state", real)
    await app.bot.handle(start(app))
    assert app.notifier.destination_id is not None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"claimed_for": true}',
        '{"claimed_for": "123:-1"}',
        '{"claimed_for": "123:0"}',
        json.dumps({"claimed_for": "123:" + "1" * 5000}),
    ],
    ids=["json", "bool", "negative", "zero", "oversized"],
)
async def test_malformed_storage_refuses_claim_and_preserves_original(unclaimed, tg, content):
    path = unclaimed.settings.state_dir / "telegram.json"
    path.write_text(content)
    again = rebuild(unclaimed, tg)
    await again.bot._setup_claim()
    await again.bot.handle(start(unclaimed))
    await again.bot._save()
    assert path.read_text() == content
    assert again.notifier.destination_id is None and not tg.sent
    await again.bot.close()


async def test_unclaimed_restart_rotates_code(unclaimed, tg, monkeypatch):
    app = unclaimed
    await app.bot._setup_claim()
    await app.bot._save()
    monkeypatch.setattr(bot_mod.secrets, "token_urlsafe", lambda size: "replacement-test-code")
    again = rebuild(app, tg)
    await again.bot._setup_claim()
    await again.bot.handle(start(app))
    assert again.notifier.destination_id is None
    await again.bot.handle(start(app, code="replacement-test-code"))
    assert again.notifier.destination_id is not None
    assert CODE not in json.dumps(asdict(load_state(app.settings.state_dir / "telegram.json")))
    await again.bot.close()


async def test_old_explicit_confirmation_is_reused_only_for_matching_claim(unclaimed, tg):
    path = unclaimed.settings.state_dir / "telegram.json"
    save_state(path, ShellState(0, "123", "123:2000002", "old-test-nonce"))
    again = rebuild(unclaimed, tg)
    await again.bot._setup_claim()
    await again.bot.handle(start(unclaimed))
    assert not again.bot.confirmed
    await again.bot.close()


def test_claim_state_write_is_private_and_does_not_follow_stale_temp_symlink(tmp_path):
    path, target = tmp_path / "telegram.json", tmp_path / "unrelated"
    target.write_text("keep this")
    path.with_name("telegram.json.tmp").symlink_to(target)
    save_state(path, ShellState(bot_id="123", claimed_for="123:1000001"))
    assert target.read_text() == "keep this"
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_state(path).claimed_for == "123:1000001"


async def test_cancelled_claim_waits_for_durable_write_before_routing(unclaimed, tg, monkeypatch):
    app = unclaimed
    await app.bot._setup_claim()
    entered, gate, _, _ = gated_saves(monkeypatch)
    handler = asyncio.create_task(app.bot.handle(start(app)))
    await until(entered.is_set)
    assert app.notifier.destination_id is None
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    gate.set()
    await asyncio.gather(*app.bot.writes)
    assert app.notifier.destination_id is not None
    assert load_state(app.settings.state_dir / "telegram.json").claimed_for == "123:1000001"


async def test_getme_failure_retries_without_stopping_watchdog(unclaimed, tg, monkeypatch):
    app = unclaimed
    monkeypatch.setattr(bot_mod, "RETRY_S", 0.01)
    good = tg.fail
    attempted = asyncio.Event()

    def flaky(method):
        if method == "getMe" and not attempted.is_set():
            attempted.set()
            return error(503, "temporary failure")
        return good(method)

    tg.fail = flaky
    task = asyncio.create_task(app.bot.run())
    try:
        await attempted.wait()
        await app.rt.watchdog_once()
        await until(lambda: tg.calls.count("getMe") == 2 and len(tg.polls) >= 2)
        assert not task.done()
        await app.bot.handle(start(app))
        assert app.notifier.destination_id is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize(
    "identity",
    [
        True,
        {},
        {"is_bot": True, "id": 456, "username": "x"},
        {"is_bot": True, "id": 123, "username": "x?start=bad"},
        {"is_bot": True, "id": 123, "username": "x" * 10000},
    ],
)
async def test_invalid_getme_response_cannot_publish_or_claim(unclaimed, tg, caplog, identity):
    from tcc_app.telegram import TelegramError

    tg.fail = lambda method: ok(identity) if method == "getMe" else None
    with pytest.raises(TelegramError, match="invalid bot identity"):
        await unclaimed.bot._setup_claim()
    await unclaimed.bot.handle(start(unclaimed))
    assert LINK not in caplog.text and not tg.sent


async def test_explicit_chat_needs_no_claim_identity_request(unclaimed, tg, caplog):
    app = rebuild(unclaimed, tg, chat_id=CHAT)
    await app.bot._setup_claim()
    assert "getMe" not in tg.calls and LINK not in caplog.text
    plain = start(unclaimed)
    plain["message"]["text"] = "/start"
    await app.bot.handle(plain)
    assert len(tg.sent) == 3 and not app.bot.confirmed
    await app.bot.close()


async def test_claim_remains_available_while_ns_startup_hangs(unclaimed, tg):
    app = unclaimed
    asked, never = asyncio.Event(), asyncio.Event()

    class Hanging(StationsProvider):
        async def stations(self):
            asked.set()
            await never.wait()
            return CODES

    waiting = build(
        app.settings,
        clock=app.rt._clock,
        provider=Hanging(app.rt._provider, CODES),
        client=tg.client(),
    )
    task = asyncio.create_task(serve(waiting, free_port()))
    try:
        await until(lambda: asked.is_set() and "getMe" in tg.calls and len(tg.polls) >= 2)
        assert not waiting.rt.started
        await waiting.bot.handle(start(waiting))
        assert waiting.notifier.destination_id is not None
        assert not waiting.bot.confirmed
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

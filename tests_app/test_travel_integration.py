import asyncio
import json
from datetime import timedelta

import httpx
import httpx2
import pytest
from app_rig import CHAT, TickClock, free_port, make_app, msg, until
from app_rig import TOKEN as BOT_TOKEN
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from runtime_rig import DAY, at
from tcc_app.main import ProcessPhase, Settings, mcp_answer, serve
from tcc_app.mcp_server import build as mcp_build
from tcc_app.travel_language import LanguageSettings
from tcc_app.web import start_readout
from test_travel_actions import TOKEN, Plans


async def test_mcp_change_applies_only_after_private_telegram_approval(tmp_path, tg, sock):
    from tcc_app.main import dispatch_mcp

    app = make_app(tmp_path, tg, TickClock(at("06:00")), mcp_agents={"desktop": TOKEN})
    fake = Plans()
    app.bot.travel_actions.rt = fake
    app.bot.state.confirmed_for = f"{BOT_TOKEN.split(':', 1)[0]}:{CHAT}"
    server = await start_readout(
        sock, mcp_answer(app), actions=lambda req, token: dispatch_mcp(app, req, token)
    )
    async with server, Client(mcp_build(sock, token=TOKEN)) as client:
        request = {"request_id": "utterance-1", "change": {"action": "create"}}
        pending = await client.call_tool("change_travel_plan", request)
        assert pending.structured_content["status"] == "pending"
        prompt = tg.sent[-1]
        data = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

        def tap(chat):
            return {
                "callback_query": {
                    "id": "tap",
                    "data": data,
                    "message": {**prompt, "chat": {"id": chat}},
                },
            }

        await app.bot.handle(tap(CHAT + 1))
        assert fake.changes == 0
        await app.bot.handle(tap(CHAT))
        assert fake.changes == 1
        receipt = await client.call_tool("get_travel_change", {"request_id": "utterance-1"})
        assert receipt.structured_content["status"] == "saved"


def test_remote_mcp_is_opt_in_and_credentials_are_not_in_repr(tmp_path):
    base = {
        "NS_API_KEY": "unused",
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "TCC_STATE_DIR": str(tmp_path),
        "TCC_MCP_AGENTS": '{"desktop":"' + TOKEN + '"}',
    }
    assert Settings.from_env(base).mcp_remote_port is None
    settings = Settings.from_env(
        {
            **base,
            "TCC_MCP_REMOTE_PORT": "8765",
            "TCC_MCP_REMOTE_ALLOWED_HOSTS": "tcc.example:8765",
        }
    )
    assert settings.mcp_remote_bind == "127.0.0.1"
    assert settings.mcp_remote_hosts == ("tcc.example:8765",)
    assert TOKEN not in repr(settings)


async def test_app_starts_and_stops_the_configured_remote_listener(tmp_path, tg):
    port = free_port()
    app = make_app(
        tmp_path,
        tg,
        TickClock(at("06:00")),
        mcp_agents={"desktop": TOKEN},
        mcp_remote_port=port,
        mcp_remote_hosts=(f"127.0.0.1:{port}",),
    )
    service = asyncio.create_task(serve(app, 0))
    try:
        await until(lambda: app.phase is ProcessPhase.RUNNING)
        url = f"http://127.0.0.1:{port}/mcp"
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(url, http_client=http)) as mcp,
        ):
            reply = await mcp.call_tool("list_travel_plans", {})
            assert reply.structured_content == {"plans": []}
    finally:
        service.cancel()
        with pytest.raises(asyncio.CancelledError):
            await service
    assert app.phase is ProcessPhase.STOPPED


async def test_private_telegram_can_plan_through_a_configured_language_service(tmp_path, tg):
    offered = []
    date = (DAY + timedelta(days=4)).isoformat()

    def adapter(request):
        offered.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "change": {
                    "action": "create",
                    "plan": {
                        "origin": "AAA",
                        "destination": "UT",
                        "start_date": date,
                        "outbound": {"mode": "window", "arrive_by": "09:00"},
                        "return": {"mode": "on_leave"},
                    },
                }
            },
        )

    tg.other = adapter
    app = make_app(
        tmp_path,
        tg,
        TickClock(at("06:00")),
        travel_language=LanguageSettings("agent", httpx.URL("https://agent.example/travel")),
    )
    try:
        await app.rt.start()
        app.bot.state.confirmed_for = f"{BOT_TOKEN.split(':', 1)[0]}:{CHAT}"
        await app.bot.handle(
            msg(app, "Utrecht by nine Saturday, and pick my train home when I leave")
        )
        assert offered[0]["text"].startswith("Utrecht by nine")
        prompt = tg.sent[-1]
        assert app.rt.list_travel_plans()["plans"] == []
        (allow,) = [
            button["callback_data"]
            for row in prompt["reply_markup"]["inline_keyboard"]
            for button in row
            if button["text"] == "Allow once"
        ]
        await app.bot.handle(
            {
                "callback_query": {
                    "id": "local-allow",
                    "data": allow,
                    "message": {**prompt, "chat": {"id": CHAT}},
                }
            }
        )
        (saved,) = app.rt.list_travel_plans()["plans"]
        assert saved["start_date"] == date
        assert saved["outbound"]["arrive_by"] == "09:00"
        assert saved["return"]["mode"] == "on_leave"
    finally:
        await app.bot.close()
        await app.follower.close()

import asyncio
import json
from datetime import timedelta

import httpx
import httpx2
import pytest
from app_rig import CHAT, CODES, TickClock, free_port, make_app, msg, until
from app_rig import TOKEN as BOT_TOKEN
from builders import make_connection, make_leg
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from runtime_rig import DAY, Provider, StationsProvider, at
from tcc_app.main import ProcessPhase, Settings, dispatch_mcp, mcp_answer, serve
from tcc_app.mcp_remote import serve_remote
from tcc_app.mcp_server import build as mcp_build
from tcc_app.travel_language import LanguageSettings
from tcc_app.web import start_readout
from test_travel_actions import TOKEN, Plans

from train_commute_companion.messages import Refused
from train_commute_companion.providers.base import ProviderError


async def test_mcp_change_applies_only_after_private_telegram_approval(tmp_path, tg, sock):
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


async def test_mcp_quick_trip_requires_a_token_and_shares_the_runtime_limit(tmp_path, tg, sock):
    clock = TickClock(at("06:00"))
    provider = Provider(clock, [make_connection([make_leg("AAA", "UT", "08:12", "08:40")])])
    app = make_app(
        tmp_path, tg, clock, StationsProvider(provider, CODES), mcp_agents={"desktop": TOKEN}
    )
    await app.rt.start()
    before = app.rt.state
    args = {"origin": "AAA", "destination": "UT"}
    request = {"tool": "check_trip", **args}
    for token in (None, "wrong"):
        assert (await dispatch_mcp(app, request, token))["status"] == "denied"
    assert (await dispatch_mcp(app, {**request, "origin": {}}, TOKEN))["status"] == "invalid"
    assert not provider.calls

    server = await start_readout(
        sock, mcp_answer(app), actions=lambda req, token: dispatch_mcp(app, req, token)
    )
    async with server, Client(mcp_build(sock, token=TOKEN)) as client:
        tool = next(tool for tool in (await client.list_tools()).tools if tool.name == "check_trip")
        assert tool.annotations.read_only_hint and tool.annotations.open_world_hint
        reply = (await client.call_tool("check_trip", args)).structured_content
        assert reply["status"] == "ok"
        assert reply["checked_at"] == clock.now().isoformat()
        assert reply["destination"]["code"] == "UT" and len(reply["options"]) == 1
        assert len(provider.calls) == 1
        refused = await app.rt.check_trip(*args.values())
        assert isinstance(refused, Refused) and "30 seconds" in refused.text
        assert len(provider.calls) == 1 and not tg.sent
        assert app.rt.state == before


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
            assert reply.structured_content["plans"] == []
            assert [p["profile_id"] for p in reply.structured_content["profiles"]] == ["p1"]
    finally:
        service.cancel()
        with pytest.raises(asyncio.CancelledError):
            await service
    assert app.phase is ProcessPhase.STOPPED


async def test_remote_previews_one_off_plan_and_distinguishes_no_match_from_provider_failure(
    tmp_path, tg
):
    when = (DAY + timedelta(days=4)).isoformat()
    clock = TickClock(at("06:00"))
    provider = Provider(clock, [])
    app = make_app(
        tmp_path,
        tg,
        clock,
        StationsProvider(provider, CODES),
        mcp_agents={"desktop": TOKEN},
    )
    await app.rt.start()
    try:
        change = {
            "action": "create",
            "plan": {
                "origin": "AAA",
                "destination": "UT",
                "start_date": when,
                "outbound": {"mode": "exact", "departure": "08:12"},
            },
        }
        review = app.rt.preview_travel_plan(change)
        saved = app.rt.save_travel_plan(change, review["review_token"], "remote-future-trip")
        args = {"plan_id": saved["plan_id"], "direction": "outbound", "date": when}
        async with serve_remote(
            lambda request, token: dispatch_mcp(app, request, token),
            lambda token: app.travel_actions.agent(token) is not None,
            host="127.0.0.1",
            port=0,
            allowed_hosts=("127.0.0.1:*",),
        ) as port:
            url = f"http://127.0.0.1:{port}/mcp"
            async with httpx.AsyncClient() as unregistered:
                rejected = await unregistered.post(
                    url,
                    json={
                        "method": "tools/call",
                        "params": {"name": "get_next_connections", "arguments": args},
                    },
                )
                assert rejected.status_code == 401
                assert not provider.calls
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
                Client(streamable_http_client(url, http_client=http)) as mcp,
            ):
                missing = await mcp.call_tool("get_next_connections", args)
                assert missing.structured_content["status"] == "no_match"
                assert missing.structured_content["fetched_at"] == clock.now().isoformat()
                assert len(provider.calls) == 1
                provider.error = lambda query: ProviderError("offline")
                unavailable = await mcp.call_tool("get_next_connections", args)
                assert unavailable.structured_content["status"] == "unavailable"
                assert unavailable.structured_content["fetched_at"] is None
                assert len(provider.calls) == 2
                no_return = await mcp.call_tool(
                    "get_next_connections", {**args, "direction": "return"}
                )
                assert no_return.is_error
                assert len(provider.calls) == 2
    finally:
        await app.bot.close()
        await app.follower.close()


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

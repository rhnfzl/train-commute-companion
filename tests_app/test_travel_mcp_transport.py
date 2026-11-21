import asyncio

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from tcc_app.mcp_server import build
from tcc_app.web import start_readout

TOKEN = "test-token-for-local-transport-1"
OTHER_TOKEN = "test-token-for-remote-transport-2"
TRAVEL_TOOLS = {
    "list_travel_plans",
    "change_travel_plan",
    "get_travel_change",
    "list_travel_preferences",
    "change_travel_preference",
}


async def test_local_relay_sends_agent_token_with_travel_requests_only(sock):
    seen = []

    def answer(request):
        seen.append(request)
        return {"status": "waiting_for_approval"}

    listener = await start_readout(sock, answer)
    assert listener is not None
    async with listener, Client(build(sock, token=TOKEN)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert TRAVEL_TOOLS <= tools.keys()
        assert tools["get_travel_change"].annotations.read_only_hint
        assert not tools["change_travel_plan"].annotations.read_only_hint
        assert tools["change_travel_plan"].annotations.idempotent_hint
        await client.call_tool("get_status", {"profile": "p1"})
        change = {"action": "cancel", "plan_id": "trip1"}
        result = await client.call_tool("change_travel_plan", {"change": change, "request_id": "a"})
        assert result.structured_content == {"status": "waiting_for_approval"}
        await client.call_tool("get_travel_change", {"request_id": "a"})

    assert seen == [
        {"tool": "get_status", "profile": "p1"},
        {"tool": "change_travel_plan", "change": change, "request_id": "a", "token": TOKEN},
        {"tool": "get_travel_change", "request_id": "a", "token": TOKEN},
    ]


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_remote_authenticates_every_request_and_checks_host_and_origin(method):
    from tcc_app.mcp_remote import serve_remote

    async def dispatch(request, token):
        raise AssertionError("Rejected request reached MCP")

    async with (
        serve_remote(
            dispatch,
            lambda token: token == TOKEN,
            host="127.0.0.1",
            port=0,
            allowed_hosts=("127.0.0.1:*",),
        ) as port,
        httpx.AsyncClient() as client,
    ):
        url = f"http://127.0.0.1:{port}/mcp"
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = await client.request(method, url, headers=headers, json={})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"

        headers = {"Authorization": f"Bearer {TOKEN}"}
        for override, status in [
            ({"Host": "wrong.example"}, 421),
            ({"Origin": "http://wrong.example"}, 403),
        ]:
            response = await client.request(method, url, headers=headers | override, json={})
            assert response.status_code == status


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_remote_mcp_uses_each_requests_token_for_tools(mode):
    from tcc_app.mcp_remote import serve_remote

    async def dispatch(request, token):
        await asyncio.sleep(0)
        return {"tool": request["tool"], "principal": {TOKEN: "work", OTHER_TOKEN: "home"}[token]}

    async with serve_remote(
        dispatch,
        lambda token: token in {TOKEN, OTHER_TOKEN},
        host="127.0.0.1",
        port=0,
        allowed_hosts=("127.0.0.1:*",),
    ) as port:
        url = f"http://127.0.0.1:{port}/mcp"

        async def get_status_for(token, name):
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
                Client(streamable_http_client(url, http_client=http), mode=mode) as c,
            ):
                assert TRAVEL_TOOLS <= {tool.name for tool in (await c.list_tools()).tools}
                result = await c.call_tool("get_travel_change", {"request_id": "a"})
                assert result.structured_content == {
                    "tool": "get_travel_change",
                    "principal": name,
                }

        await asyncio.gather(get_status_for(TOKEN, "work"), get_status_for(OTHER_TOKEN, "home"))

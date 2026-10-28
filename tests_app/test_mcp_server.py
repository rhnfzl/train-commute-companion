import json
import os
import sys
from pathlib import Path

import httpx
import pytest

mcp = pytest.importorskip("mcp")  # only this module; the 10.4 gate never skips

from builders import ONE_TRANSFER, make_connection, make_leg
from conftest import CHAT, CODES, TOKEN, StationsProvider, TickClock, make_app
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from tcc_app.main import mcp_answer
from tcc_app.mcp_server import NOT_RUNNING, build
from tcc_app.web import start_readout
from test_runtime import Provider as FakeProvider
from test_runtime import at

from train_commute_companion.providers.base import AuthError
from train_commute_companion.store import StorageError

TOOLS = {"get_status", "get_next_connections", "get_works_ahead"}
CALLS = [
    ("get_status", {"profile": "p1"}),
    ("get_next_connections", {"profile": "p1", "direction": "outbound"}),
    ("get_next_connections", {"profile": "p1", "direction": "return"}),
    ("get_works_ahead", {"profile": "p1"}),
]


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


async def test_exactly_three_read_only_tools(served, sock):
    async with Client(build(sock)) as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        a = t.annotations
        assert a.read_only_hint and not a.destructive_hint, t.name
        assert a.idempotent_hint and not a.open_world_hint, t.name
        assert t.description
    nxt = next(t for t in tools if t.name == "get_next_connections")
    assert nxt.input_schema["properties"]["direction"]["enum"] == ["outbound", "return"]


async def test_tools_return_the_readout(served, sock):
    app = served
    async with Client(build(sock)) as c:
        for name, args in CALLS:
            result = await c.call_tool(name, args)
            assert not result.is_error, text_of(result)
            want = mcp_answer(app)({"tool": name, **args})
            assert result.structured_content == want
            assert result.structured_content["text"].endswith("Data: NS")


async def test_tool_says_the_app_is_not_running(sock):  # Review Focus 1
    for stale in (False, True):
        if stale:
            sock.write_text("")  # a crash left a file where the socket was
        async with Client(build(sock)) as c:
            for name, args in CALLS:
                result = await c.call_tool(name, args)
                assert result.is_error and NOT_RUNNING in text_of(result)
                assert not result.structured_content


async def test_unknown_profile_is_a_tool_error(served, sock):
    async with Client(build(sock)) as c:
        result = await c.call_tool("get_works_ahead", {"profile": "nobody"})
        bad = await c.call_tool("get_next_connections", {"profile": "p1", "direction": "sideways"})
    assert result.is_error and "No profile nobody." in text_of(result)
    assert bad.is_error


SECRETS = {
    "ns_key": "test-ns-key-0001",
    "owntracks_secret": "test-owntracks-secret-0001",
    "health_token": "test-health-token-0001",
    "webhook_urls": ("https://hooks.example.invalid/tcc",),
    "webhook_secret": "test-webhook-secret-0001",
    "webhook_token": "test-webhook-token-0001",
}
CANCELLED_USUAL = (
    make_connection(
        [
            make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True),
            make_leg("UT", "RTD", "08:46", "09:20", number="5678"),
        ]
    ),
    *ONE_TRANSFER[1:],
)


async def test_no_secret_or_url_in_any_tool_output(tmp_path, tg, sock):  # Review Focus 5
    tg.other = lambda request: httpx.Response(200)
    clock = TickClock(at("07:50"))
    provider = FakeProvider(clock, list(ONE_TRANSFER))
    app = make_app(tmp_path, tg, clock, StationsProvider(provider, CODES), **SECRETS)
    await app.rt.start()
    server = await start_readout(sock, mcp_answer(app))
    assert server is not None
    banned = [
        "test-ns-key-0001", TOKEN, str(CHAT), "test-owntracks-secret-0001",
        "test-health-token-0001", "test-webhook-secret-0001", "test-webhook-token-0001",
        "hooks.example.invalid",
    ]  # fmt: skip
    seen: list[str] = []

    async def every_tool() -> None:
        async with Client(build(sock)) as c:
            for name, args in [
                *CALLS,
                ("get_status", {"profile": "nobody"}),
                ("get_status", {"profile": ""}),
                ("get_next_connections", {"profile": "", "direction": "outbound"}),
                ("get_works_ahead", {"profile": ""}),
            ]:
                r = await c.call_tool(name, args)
                seen.append(json.dumps(r.structured_content) + "\n" + text_of(r))

    def store_fails(s: object) -> None:
        raise StorageError("disk full")

    await every_tool()  # fresh start
    provider.world = list(CANCELLED_USUAL)
    await app.rt.poll_once()  # an alert for the cancelled usual train
    await app.rt.watchdog_once()
    await app.rt.webhooks_idle()
    assert any("cancelled" in m["text"] for m in tg.sent)
    await every_tool()
    app.rt._store.save = store_fails
    await app.rt.poll_once()
    assert not app.rt.view().store_ok
    await every_tool()
    provider.error = lambda q: AuthError("rejected")
    await app.rt.poll_once()
    assert app.rt.view().key_rejected
    await every_tool()
    profiles = tmp_path / "profiles.yaml"
    yaml = profiles.read_text()
    profiles.write_text(yaml.replace('name: "office_a"', 'name: "office_a"\n    traveller: "test-ns-key-0001"'))  # fmt: skip
    await app.rt.reload()
    assert app.rt.view().refusal is not None
    await every_tool()
    server.close()
    await server.wait_closed()
    assert len(seen) == 5 * 8
    for out in seen:
        for value in banned:
            assert value not in out, (value, out)


async def test_stdio_entry_over_a_real_pipe(served, sock):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tcc_app.mcp_server"],
        env={"TCC_STATE_DIR": str(sock.parent), "PYTHONPATH": "app", "PATH": os.environ["PATH"]},
    )
    async with Client(params) as c:
        tools = (await c.list_tools()).tools
        result = await c.call_tool("get_status", {"profile": "p1"})
    assert {t.name for t in tools} == TOOLS
    assert not result.is_error, text_of(result)
    assert result.structured_content["text"].endswith("Data: NS")


def test_stdio_server_reads_no_setting():
    src = Path("app/tcc_app/mcp_server.py").read_text()
    assert "Settings" not in src and "from_env" not in src and "environ[" not in src

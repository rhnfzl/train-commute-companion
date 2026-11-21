import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

from app_rig import CHAT, CODES, STRANGER, TickClock, make_app
from builders import make_connection, make_leg
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from runtime_rig import DAY, Provider, StationsProvider, at, usual_on
from tcc_app.main import dispatch_mcp, mcp_answer
from tcc_app.mcp_server import build as mcp_server
from tcc_app.web import ask_readout, start_readout

from train_commute_companion.model import Provenance

FIRST = DAY + timedelta(days=7)
SKIPPED = DAY + timedelta(days=14)
LAST = DAY + timedelta(days=21)
AGENT_TOKEN = "test-desktop-agent-token-with-enough-characters"


@asynccontextmanager
async def started_app(tmp_path, tg, clock, provider=None, **settings):
    app = make_app(tmp_path, tg, clock, provider=provider, **settings)
    await app.rt.start()
    try:
        yield app
    finally:
        await app.bot.close()
        await app.follower.close()


async def message(app, tg, text):
    sent = len(tg.sent)
    await app.bot.handle(
        {"message": {"chat": {"id": CHAT}, "text": text, "date": int(app.rt.now().timestamp())}}
    )
    assert len(tg.sent) > sent
    return tg.sent[-1]


async def tap(app, tg, prompt, label, *, chat=CHAT):
    keyboard = prompt.get("reply_markup", {}).get("inline_keyboard", [])
    data = next((b["callback_data"] for row in keyboard for b in row if b["text"] == label), None)
    assert data is not None, (label, prompt)
    sent = len(tg.sent)
    await app.bot.handle(
        {
            "callback_query": {
                "id": f"tap-{sent}",
                "data": data,
                "message": {**prompt, "chat": {"id": chat}, "message_id": sent + 1},
            }
        }
    )
    if chat != CHAT:
        assert len(tg.sent) == sent
        return None
    assert len(tg.sent) > sent
    return tg.sent[-1]


async def confirm_delivery(app, tg):
    prompt = await message(app, tg, "/test")
    reply = await tap(app, tg, prompt, "I got both")
    assert "Delivery confirmed" in reply["text"]


async def test_authenticated_stdio_lists_both_schedules_and_previews_saved_journeys(
    tmp_path, tg, sock
):
    clock = TickClock(at("06:00"))
    tomorrow = FIRST + timedelta(days=1)

    def train(dep, arr, number, **kwargs):
        leg = make_leg("AAA", "UT", dep, arr, number=number, day=FIRST, **kwargs)
        if kwargs.get("provenance") is Provenance.PLANNED:
            leg = replace(
                leg,
                stops=(
                    replace(leg.board, departure=replace(leg.board.departure, value=None)),
                    replace(leg.alight, arrival=replace(leg.alight.arrival, value=None)),
                ),
            )
        return make_connection([leg])

    trains = Provider(
        clock,
        [
            train("08:05", "08:55", "8800", delay_arr=20),
            train("08:12", "08:40", "8801", provenance=Provenance.PLANNED),
            make_connection([make_leg("UT", "AAA", "00:20", "01:03", number="8910", day=FIRST)]),
            make_connection([make_leg("UT", "AAA", "00:20", "01:15", number="8911", day=FIRST)]),
        ],
    )
    async with started_app(
        tmp_path, tg, clock, StationsProvider(trains, CODES), mcp_agents={"desktop": AGENT_TOKEN}
    ) as app:
        change = {
            "action": "create",
            "plan": {
                "name": "Utrecht visits",
                "origin": "AAA",
                "destination": "UT",
                "start_date": FIRST.isoformat(),
                "weekdays": [FIRST.isoweekday()],
                "end_date": LAST.isoformat(),
                "skip_dates": [SKIPPED.isoformat()],
                "outbound": {"mode": "window", "earliest": "08:00", "arrive_by": "09:00"},
                "return": {
                    "mode": "exact",
                    "departure": "00:20",
                    "train_number": "8910",
                    "day_offset": 1,
                },
            },
        }
        review = app.rt.preview_travel_plan(change)
        if review["status"] == "conflict":
            change["conflict_policy"] = "replace"
            review = app.rt.preview_travel_plan(change)
        saved = app.rt.save_travel_plan(change, review["review_token"], "future-mcp-test")
        plan_id = saved["plan_id"]
        before = app.rt.state
        no_calls = len(trains.calls)
        listener = await start_readout(
            sock, mcp_answer(app), actions=lambda req, token: dispatch_mcp(app, req, token)
        )
        base = {
            "tool": "get_next_connections",
            "direction": "outbound",
            "profile": plan_id,
            "date": FIRST.isoformat(),
        }
        async with listener:
            for token in (None, "wrong"):
                reply = await ask_readout(sock, base if token is None else {**base, "token": token})
                assert "error" in reply
            assert len(trains.calls) == no_calls
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "tcc_app.mcp_server"],
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": "app",
                    "TCC_STATE_DIR": str(sock.parent),
                    "TCC_MCP_AGENT_TOKEN": AGENT_TOKEN,
                },
            )
            async with Client(params) as client:
                listed = (await client.call_tool("list_travel_plans", {})).structured_content
                assert [p["profile_id"] for p in listed["profiles"]] == ["p1"]
                assert [p["plan_id"] for p in listed["plans"]] == [plan_id]

                result = (
                    await client.call_tool(
                        "get_next_connections",
                        {"profile": plan_id, "direction": "outbound", "date": FIRST.isoformat()},
                    )
                ).structured_content
                assert result["source"] == "ns"
                assert result["status"] == "available"
                assert result["date"] == FIRST.isoformat()
                (journey,) = result["journeys"]
                (leg,) = journey["next_good"]["legs"]
                assert leg["train_number"] == "8801"
                assert leg["planned_arrival"] == "08:40"
                assert leg["arrival"] is None
                assert trains.calls[-1].search_for_arrival

                back = (
                    await client.call_tool(
                        "get_next_connections",
                        {
                            "plan_id": plan_id,
                            "direction": "return",
                        },
                    )
                ).structured_content
                (journey,) = back["journeys"]
                assert journey["service_date"] == tomorrow.isoformat()
                assert journey["next_good"]["legs"][0]["train_number"] == "8910"

                for date in (SKIPPED.isoformat(), (DAY + timedelta(days=31)).isoformat()):
                    calls = len(trains.calls)
                    failed = await client.call_tool(
                        "get_next_connections",
                        {
                            "plan_id": plan_id,
                            "direction": "outbound",
                            "date": date,
                        },
                    )
                    assert failed.is_error
                    assert len(trains.calls) == calls
                assert app.rt.state == before

                clock.t = at("00:05", tomorrow)
                await app.rt.poll_once()
                calls = len(trains.calls)
                cached = await client.call_tool(
                    "get_next_connections",
                    {"plan_id": plan_id, "direction": "return", "date": FIRST.isoformat()},
                )
                assert cached.structured_content["source"] == "cache"
                assert len(trains.calls) == calls


async def test_guided_temporary_replacement_persists_and_undo_restores_usual_travel(tmp_path, tg):
    clock = TickClock(at("06:00"))
    trains = Provider(clock, [])
    provider = StationsProvider(trains, CODES)
    async with started_app(tmp_path, tg, clock, provider) as app:
        regular_profile = app.settings.profiles.read_bytes()
        await confirm_delivery(app, tg)
        wizard = await message(app, tg, "/plan")
        await tap(app, tg, wizard, "New plan")
        await message(app, tg, "AAA")
        await message(app, tg, "UT")
        start = await message(app, tg, FIRST.isoformat())
        await tap(app, tg, start, "Repeating")
        await message(app, tg, "Tue")
        await message(app, tg, "1")
        await message(app, tg, LAST.isoformat())
        mode = await message(app, tg, SKIPPED.isoformat())
        await tap(app, tg, mode, "Exact train")
        return_day = await message(app, tg, "08:12 8801")
        return_mode = await tap(app, tg, return_day, "Next day")
        await tap(app, tg, return_mode, "Exact train")
        conflict = await message(app, tg, "00:20 8910")
        assert FIRST.isoformat() in conflict["text"] and LAST.isoformat() in conflict["text"]
        assert SKIPPED.isoformat() not in conflict["text"]
        assert all(
            button["text"] != "Keep both"
            for row in conflict["reply_markup"]["inline_keyboard"]
            for button in row
        )
        assert app.rt.list_travel_plans()["plans"] == []

        preview = await tap(app, tg, conflict, "Replace overlapping trips")
        saved_message = await tap(app, tg, preview, "Save plan")
        (plan,) = app.rt.list_travel_plans()["plans"]
        assert plan["skip_dates"] == [SKIPPED.isoformat()]
        assert plan["return"]["day_offset"] == 1
        assert app.settings.profiles.read_bytes() == regular_profile
        plan_id = plan["plan_id"]

    clock.t = at("07:55", FIRST)
    trains.world = [
        make_connection([make_leg("AAA", "UT", "08:12", "08:38", number=number, day=FIRST)])
        for number in ("8800", "8801")
    ]
    async with started_app(tmp_path, tg, clock, provider) as reopened:
        assert reopened.bot.confirmed
        assert reopened.rt.list_travel_plans()["plans"][0]["plan_id"] == plan_id
        await reopened.rt.poll_once()
        key = f"{plan_id}|outbound|{FIRST}"
        occurrence = reopened.rt.state.occurrences[key]
        assert occurrence.lock is not None
        assert occurrence.lock.legs[0].train.train_number == "8801"
        assert f"p1|outbound|{FIRST}" not in reopened.rt.state.occurrences

        tomorrow = FIRST + timedelta(days=1)
        clock.t = at("00:12", tomorrow)
        trains.world = [
            make_connection([make_leg("UT", "AAA", "00:20", "01:03", number="8910", day=FIRST)])
        ]
        await reopened.rt.poll_once()
        occurrence = reopened.rt.state.occurrences[f"{plan_id}|return|{tomorrow}"]
        assert occurrence.lock is not None
        assert occurrence.lock.legs[0].train.train_number == "8910"

        clock.t = at("07:55", SKIPPED)
        trains.world = [usual_on(SKIPPED)]
        await reopened.rt.poll_once()
        assert f"p1|outbound|{SKIPPED}" in reopened.rt.state.occurrences
        assert f"{plan_id}|outbound|{SKIPPED}" not in reopened.rt.state.occurrences

        clock.t = at("07:55", LAST)
        trains.world = [usual_on(LAST)]
        await reopened.rt.poll_once()
        assert f"{plan_id}|outbound|{LAST}" in reopened.rt.state.occurrences
        assert f"p1|outbound|{LAST}" not in reopened.rt.state.occurrences
        await tap(reopened, tg, saved_message, "Undo")
        assert reopened.rt.list_travel_plans()["plans"] == []
        await reopened.rt.poll_once()
        assert f"p1|outbound|{LAST}" in reopened.rt.state.occurrences


async def test_authenticated_mcp_changes_wait_for_private_approval_and_persist_receipts(
    tmp_path, tg, sock
):
    clock = TickClock(at("06:00"))
    agents = {"desktop": AGENT_TOKEN}
    request = {
        "request_id": "weekend-visit",
        "change": {
            "action": "create",
            "plan": {
                "name": "Weekend visit",
                "origin": "AAA",
                "destination": "UT",
                "start_date": (DAY + timedelta(days=4)).isoformat(),
                "outbound": {"mode": "exact", "departure": "08:12", "train_number": "8801"},
            },
        },
    }
    async with started_app(tmp_path, tg, clock, mcp_agents=agents) as app:
        await confirm_delivery(app, tg)
        server = await start_readout(
            sock, mcp_answer(app), actions=lambda req, token: dispatch_mcp(app, req, token)
        )
        assert server is not None
        async with server, Client(mcp_server(sock, token=AGENT_TOKEN)) as client:
            pending = await client.call_tool("change_travel_plan", request)
            assert pending.structured_content["status"] == "pending"
            prompt = tg.sent[-1]
            assert "Weekend visit" in prompt["text"]
            assert app.rt.list_travel_plans()["plans"] == []
            await tap(app, tg, prompt, "Allow once", chat=STRANGER)
            assert app.rt.list_travel_plans()["plans"] == []
            approved_message = await tap(app, tg, prompt, "Allow once")
            assert any(
                button["text"] == "Undo"
                for row in approved_message.get("reply_markup", {}).get("inline_keyboard", [])
                for button in row
            ), approved_message
            receipt = await client.call_tool(
                "get_travel_change", {"request_id": request["request_id"]}
            )
            assert receipt.structured_content["status"] == "saved"
            plan_id = receipt.structured_content["plan_id"]
            assert app.rt.list_travel_plans()["plans"][0]["plan_id"] == plan_id
            repeat = await client.call_tool("change_travel_plan", request)
            assert repeat.structured_content == receipt.structured_content
            assert len(app.rt.list_travel_plans()["plans"]) == 1
        sock.unlink(missing_ok=True)

    async with started_app(tmp_path, tg, clock, mcp_agents=agents) as reopened:
        server = await start_readout(
            sock,
            mcp_answer(reopened),
            actions=lambda req, token: dispatch_mcp(reopened, req, token),
        )
        assert server is not None
        async with server, Client(mcp_server(sock, token=AGENT_TOKEN)) as client:
            recovered = await client.call_tool(
                "get_travel_change", {"request_id": request["request_id"]}
            )
            assert recovered.structured_content == receipt.structured_content
            assert reopened.rt.list_travel_plans()["plans"][0]["plan_id"] == plan_id
            assert reopened.bot.travel_actions.policies() == {"desktop": "ask"}
            await tap(reopened, tg, approved_message, "Undo")
            assert reopened.rt.list_travel_plans()["plans"] == []

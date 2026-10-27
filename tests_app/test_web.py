import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import StationsProvider, TickClock
from tcc_app.web import BODY_LIMIT, health_for, health_report, start_web
from test_main import CODES, make_app
from test_runtime import Provider, at, every_day

from train_commute_companion.view import RuntimeView

FINE = RuntimeView(at("07:50"), True, (), (), (), {}, (), None, True, False, 0, 3)


@pytest.mark.parametrize(
    ("broken", "key", "value"),
    [
        ("dead", "alive", False),
        ("not_started", "started", False),
        ("store", "store_ok", False),
        ("key", "key_rejected", True),
        ("refusal", "profile_refused", "p1: bad station"),
        ("undelivered", "undelivered", 2),
        ("unconfirmed", "delivery_confirmed", False),
    ],
)
def test_health_is_503_naming_each_problem(broken, key, value):
    status, body = health_report(FINE, True, True)
    assert status == 200 and body["ok"] is True and body["attribution"] == "Data: NS"
    view = {
        "not_started": replace(FINE, started=False),
        "store": replace(FINE, store_ok=False),
        "key": replace(FINE, key_rejected=True),
        "refusal": replace(FINE, refusal="p1: bad station"),
        "undelivered": replace(FINE, undelivered=2),
    }.get(broken, FINE)
    status, body = health_report(view, broken != "dead", broken != "unconfirmed")
    assert status == 503 and body["ok"] is False and body[key] == value


def test_health_shows_lookahead_last_ok_without_failing():
    old = at("06:00") - timedelta(days=3)
    view = replace(FINE, lookahead_last_ok={"p1|outbound": old, "p1|return": None})
    status, body = health_report(view, True, True)
    assert status == 200
    assert body["lookahead_last_ok"] == {"p1|outbound": old.isoformat(), "p1|return": None}


async def test_health_lists_occurrences_of_a_built_app(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, StationsProvider(Provider(clock, every_day()), CODES))
    await app.rt.start()
    await app.rt.poll_once()
    status, body = health_report(app.rt.view(), True, app.bot.confirmed)
    assert status == 503 and body["delivery_confirmed"] is False  # /test not tapped yet
    assert body["profile_refused"] is None and body["requests"] > 0
    out = next(o for o in body["occurrences"] if o["direction"] == "outbound")
    assert out == {
        "profile_id": "p1", "direction": "outbound", "service_date": "2026-10-27",
        "state": "locked", "fresh": True, "last_ok_at": at("07:50").isoformat(),
        "consecutive_failures": 0, "auth_failed": False,
    }  # fmt: skip
    json.dumps(body)


async def ask(port: int, raw: bytes) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, body = answer.partition(b"\r\n\r\n")
    assert b"Connection: close" in head
    return int(head.split(b" ")[1]), body


async def test_health_endpoint_and_unknown_path():
    answer = (503, {"ok": False})
    server = await start_web(0, health=lambda: answer, host="127.0.0.1")
    port = server.sockets[0].getsockname()[1]
    try:
        status, body = await ask(port, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        assert (status, json.loads(body)) == (503, {"ok": False})
        answer = (200, {"ok": True})
        status, body = await ask(port, b"GET /health?x=1 HTTP/1.1\r\n\r\n")
        assert (status, json.loads(body)) == (200, {"ok": True})
        assert (await ask(port, b"GET /nope HTTP/1.1\r\n\r\n"))[0] == 404
        assert (await ask(port, b"POST /health HTTP/1.1\r\nContent-Length: 0\r\n\r\n"))[0] == 404
        assert (await ask(port, b"garbage\r\n\r\n"))[0] == 400
        assert (await ask(port, b"GET /health HTTP/1.1\r\nno colon\r\n\r\n"))[0] == 400
        huge = b"GET /health HTTP/1.1\r\nX: " + b"a" * 17000 + b"\r\n\r\n"
        assert (await ask(port, huge))[0] == 400
        big = f"POST /owntracks HTTP/1.1\r\nContent-Length: {BODY_LIMIT + 1}\r\n\r\n".encode()
        assert (await ask(port, big))[0] == 413
    finally:
        server.close()
        await server.wait_closed()


FULL = (503, {"ok": False, "attribution": "Data: NS", "problems": ["x"]})


@pytest.mark.parametrize("peer", [("127.0.0.1", 5000), ("::1", 5000, 0, 0)])
def test_health_full_body_for_a_loopback_peer(peer):
    assert health_for(peer, *FULL) == FULL


@pytest.mark.parametrize("peer", [("203.0.113.9", 5000), ("2001:db8::1", 5000, 0, 0), None, ""])
def test_health_is_ok_only_for_any_other_peer_with_the_same_status(peer):
    assert health_for(peer, *FULL) == (503, {"ok": False})
    assert health_for(peer, 200, {"ok": True, "x": 1}) == (200, {"ok": True})


async def test_health_endpoint_gives_a_loopback_caller_the_full_body():
    server = await start_web(0, health=lambda: FULL, host="127.0.0.1")
    try:
        status, body = await ask(
            server.sockets[0].getsockname()[1], b"GET /health HTTP/1.1\r\n\r\n"
        )
        assert (status, json.loads(body)) == FULL
    finally:
        server.close()
        await server.wait_closed()

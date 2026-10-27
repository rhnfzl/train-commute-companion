import asyncio
import base64
import json
from dataclasses import replace
from datetime import date

import pytest
from builders import only_occurrence
from conftest import TickClock, spied
from tcc_app.follow import Fix
from tcc_app.main import serve
from tcc_app.owntracks import handler, parse
from tcc_app.web import BODY_LIMIT, start_web
from test_follow import FAR, UT, app_owntracks  # noqa: F401 (fixture)
from test_main import free_port, make_app, until
from test_runtime import PROFILES_DIR, at

from train_commute_companion.calendar import TZ
from train_commute_companion.profile import check_lines

SECRET = "test-secret"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"[]",
        b'{"_type":"transition","event":"enter","desc":"home"}',
        b'{"_type":"lwt","tst":1700000000}',
        b'{"_type":"location","lat":52.1,"lon":5.1}',
        b'{"_type":"location","lat":52.1,"lon":5.1,"tst":99999999999999999999}',
        b'{"_type":"location","lat":"52.1","lon":5.1,"tst":1700000000}',
        b'{"_type":"location","lat":NaN,"lon":5.1,"tst":1700000000}',
        b'{"_type":"location","lat":52.1,"lon":5.1,"tst":1700000000.5}',
        b'{"_type":"location","lat":52.1,"lon":5.1,"tst":true}',
        b'{"_type":"location","lat":52.1,"lon":5.1,"tst":1700000000,"acc":"bad"}',
        b"\xff\xfe",
    ],
)
def test_parse_ignores(body):
    assert parse(body) is None


def test_parse_location():
    tst = int(at("08:24").timestamp())
    body = {"_type": "location", "lat": 52.1, "lon": 5, "tst": tst, "acc": 12,
            "inregions": ["home_area", 7, "work_area"]}  # fmt: skip
    fix = parse(json.dumps(body).encode())
    assert fix == Fix(52.1, 5.0, at("08:24"), 12.0, frozenset({"home_area", "work_area"}))
    assert fix.at.tzinfo is TZ
    bare = parse(json.dumps({"_type": "location", "lat": 52.1, "lon": 5.1, "tst": tst}).encode())
    assert bare == Fix(52.1, 5.1, at("08:24"))


def test_owntracks_profile_passes_check_lines():
    text = (PROFILES_DIR / "located_via_ut_owntracks.yaml").read_text()
    cfg, lines = check_lines(text, frozenset({"AAA", "UT", "RTD"}), date(2026, 10, 27))
    assert cfg is not None and lines == []


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def post(port: int, body: bytes, auth: str | None = None,
               length: int | None = None) -> tuple[int, bytes, bytes]:  # fmt: skip
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = (
        f"POST /owntracks HTTP/1.1\r\nContent-Length: {len(body) if length is None else length}\r\n"
    )
    if auth is not None:
        head += f"Authorization: {auth}\r\n"
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    top, _, payload = answer.partition(b"\r\n\r\n")
    return int(top.split(b" ")[1]), top, payload


async def test_owntracks_endpoint_auth_and_reply():
    got: list[bytes] = []
    server = await start_web(0, health=lambda: (200, {}), owntracks=got.append, secret=SECRET,
                             host="127.0.0.1")  # fmt: skip
    port = server.sockets[0].getsockname()[1]
    plain = await start_web(0, health=lambda: (200, {}), owntracks=got.append, host="127.0.0.1")
    plain_port = plain.sockets[0].getsockname()[1]
    try:
        status, head, _ = await post(port, b"{}")
        assert status == 401 and b'WWW-Authenticate: Basic realm="tcc"' in head
        assert (await post(port, b"{}", basic("me", "wrong")))[0] == 401
        assert (await post(port, b"{}", "Basic !!!"))[0] == 401
        assert (await post(port, b"{}", "Bearer " + SECRET))[0] == 401
        assert got == []
        status, head, payload = await post(port, b'{"_type":"lwt"}', basic("anyone", SECRET))
        assert (status, payload) == (200, b"[]") and b"Content-Type: application/json" in head
        assert got == [b'{"_type":"lwt"}']
        too_big = BODY_LIMIT + 1024  # 65 KiB, refused before the body is read
        assert (await post(port, b"", basic("anyone", SECRET), length=too_big))[0] == 413
        assert (await post(plain_port, b"{}", basic("anyone", SECRET)))[0] == 404
    finally:
        for s in (server, plain):
            s.close()
            await s.wait_closed()


async def test_owntracks_post_produces_a_zone_event(app_owntracks):  # noqa: F811
    app = app_owntracks
    spied(app)
    server = await start_web(0, health=lambda: (200, {}), owntracks=handler(app.follower),
                             secret=SECRET, host="127.0.0.1")  # fmt: skip
    port = server.sockets[0].getsockname()[1]

    async def send(lat: float, lon: float, hhmm: str, regions: list[str]) -> None:
        clock: TickClock = app.clock
        clock.t = at(hhmm)
        body = {"_type": "location", "lat": lat, "lon": lon, "tst": int(at(hhmm).timestamp()),
                "inregions": regions}  # fmt: skip
        assert (await post(port, json.dumps(body).encode(), basic("u", SECRET)))[:1] == (200,)
        await app.follower.drain()

    try:
        await send(*FAR, "07:41", [])
        await send(*UT, "07:42", [])
        assert "UT" in only_occurrence(app.rt).zone_reports  # 10.4: OwnTracks gives a zone event
        await send(*FAR, "07:43", ["work_area"])
        assert "work_area" in only_occurrence(app.rt).zone_reports  # the name `_arrived` reads
        assert spied(app) == [("UT", "enter"), ("UT", "leave"), ("work_area", "enter")]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(("secret", "expected"), [(None, 404), (SECRET, 200)])
async def test_serve_wires_owntracks_only_with_a_secret(tmp_path, tg, secret, expected):
    app = make_app(tmp_path, tg, TickClock(at("07:50")))
    app.settings = replace(app.settings, owntracks_secret=secret)
    fed: list[str] = []
    app.follower.feed = lambda source, fix: fed.append(source)
    port = free_port()
    task = asyncio.create_task(serve(app, port))
    await until(lambda: app.rt.started)
    body = json.dumps({"_type": "location", "lat": 52.1, "lon": 5.1, "tst": 1_800_000_000})
    assert (await post(port, body.encode(), basic("u", SECRET)))[0] == expected
    assert fed == (["owntracks"] if secret else [])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

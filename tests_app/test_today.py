import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from app_rig import occ, view
from tcc_app.today import render
from tcc_app.web import start_web

from train_commute_companion.providers.base import ActiveDisruption

TOKEN = "example-test-today-token"


async def request(port, path="/today", authorization=None, method="GET"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    header = f"Authorization: {authorization}\r\n" if authorization is not None else ""
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n{header}\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:]), body.decode()


@asynccontextmanager
async def served(token=TOKEN, *, snapshot=None):
    calls = []

    def today():
        calls.append(True)
        return snapshot if snapshot is not None else view(occ()), True

    server = await start_web(
        0, health=lambda: (200, {"ok": True}), host="127.0.0.1", health_token=token, today=today
    )
    try:
        yield server.sockets[0].getsockname()[1], calls
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic !!!"])
async def test_today_requires_auth_before_reading_private_view(authorization):
    async with served() as (port, calls):
        status, headers, body = await request(port, authorization=authorization)
        assert status == 401 and calls == []
        assert headers["WWW-Authenticate"] == 'Basic realm="tcc-today"'
        assert body == "Authentication required."
        assert "office" not in body and TOKEN not in body
        assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer " + TOKEN,
        "Basic " + base64.b64encode(("viewer:" + TOKEN).encode()).decode(),
    ],
)
async def test_today_serves_private_read_only_html_for_both_auth_methods(authorization):
    async with served() as (port, calls):
        status, headers, body = await request(port, authorization=authorization)
        assert status == 200 and calls == [True]
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "style-src 'unsafe-inline'" in headers["Content-Security-Policy"]
        assert "<h1>Today</h1>" in body and "Office A" in body
        assert 'href="/today"' in body and TOKEN not in body
        assert "<script" not in body and "<form" not in body
        assert "last successful NS check" in body


async def test_today_is_disabled_without_token_and_never_uses_query_credentials():
    async with served(None) as (port, calls):
        assert (await request(port, authorization="Bearer " + TOKEN))[0] == 404
        assert calls == []
    async with served() as (port, calls):
        status, _, body = await request(port, "/today?token=" + TOKEN)
        assert status == 401 and calls == [] and TOKEN not in body


def test_today_escapes_profile_ns_and_refusal_text():
    payload = '<img src=x onerror="alert(1)">'
    incident = ActiveDisruption("unsafe", payload, frozenset({"UT"}), None)
    snapshot = view(occ(profile_name=payload, disruptions=(incident,)), refusal=payload)
    result = render(snapshot, False)
    assert payload not in result
    assert "&lt;img" in result and "&quot;" in result
    assert "Delivery not confirmed" in result
    assert "not followed" in result


def test_today_shows_stale_provider_data_without_claiming_live_progress():
    occurrence = occ()
    occurrence = replace(occurrence, health=replace(occurrence.health, fresh=False))
    result = render(view(occurrence), True)
    assert "not fresh" in result and "07:50" in result
    assert "Snapshot" in result


async def test_today_has_no_post_action():
    async with served() as (port, calls):
        status, _, _ = await request(port, authorization="Bearer " + TOKEN, method="POST")
        assert status == 404 and calls == []


def calendar_occurrence():
    from runtime_rig import at

    occurrence = occ()
    return replace(
        occurrence,
        legs=(
            replace(occurrence.legs[0], planned_arrival=at("08:25")),
            replace(occurrence.legs[1], planned_arrival=at("09:00")),
        ),
    )


async def test_calendar_download_authenticates_before_reading_the_view():
    path = "/calendar/" + "a" * 64 + ".ics"
    async with served() as (port, calls):
        status, headers, body = await request(port, path)
        assert status == 401 and calls == [] and "office" not in body
        assert headers["Cache-Control"] == "no-store"
    async with served(None) as (port, calls):
        assert (await request(port, path))[0] == 404 and calls == []


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer " + TOKEN,
        "Basic " + base64.b64encode(("viewer:" + TOKEN).encode()).decode(),
    ],
)
async def test_today_link_downloads_exactly_one_selected_plan_with_existing_auth(authorization):
    from train_commute_companion.calendar_export import export_id

    occurrence = calendar_occurrence()
    snapshot = view(occurrence)
    identifier = export_id(occurrence.key)
    path = f"/calendar/{identifier}.ics"
    html = render(snapshot, True)
    assert f'href="{path}"' in html
    assert "Download this journey to calendar" in html
    assert "not live" in html
    async with served(snapshot=snapshot) as (port, calls):
        status, headers, body = await request(port, path, authorization=authorization)
        assert status == 200 and calls == [True]
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"
        assert headers["Content-Disposition"] == 'attachment; filename="2026-10-27.ics"'
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Frame-Options"] == "DENY"
        assert body.count("BEGIN:VEVENT\r\n") == 1 and "RRULE" not in body
        assert TOKEN not in body


async def test_unknown_and_unexportable_current_calendar_plans_return_404():
    from train_commute_companion.calendar_export import export_id

    occurrence = replace(calendar_occurrence(), not_travelling=True)
    snapshot = view(occurrence)
    assert "Download this journey to calendar" not in render(snapshot, True)
    async with served(snapshot=snapshot) as (port, calls):
        unknown = "/calendar/" + "f" * 64 + ".ics"
        assert (await request(port, unknown, authorization="Bearer " + TOKEN))[0] == 404
        path = f"/calendar/{export_id(occurrence.key)}.ics"
        assert (await request(port, path, authorization="Bearer " + TOKEN))[0] == 404
        assert calls == [True, True]


async def test_recovery_selection_hides_and_refuses_original_train_calendar():
    from recovery_sample import selection

    from train_commute_companion.calendar_export import export_id

    occurrence = replace(calendar_occurrence(), selected_recovery=selection())
    snapshot = view(occurrence)
    assert "Download this journey to calendar" not in render(snapshot, True)
    async with served(snapshot=snapshot) as (port, _):
        path = f"/calendar/{export_id(occurrence.key)}.ics"
        assert (await request(port, path, authorization="Bearer " + TOKEN))[0] == 404


async def test_query_credentials_cannot_download_a_calendar_plan():
    from train_commute_companion.calendar_export import export_id

    occurrence = calendar_occurrence()
    path = f"/calendar/{export_id(occurrence.key)}.ics?token={TOKEN}"
    async with served(snapshot=view(occurrence)) as (port, calls):
        status, _, body = await request(port, path)
        assert status == 401 and calls == [] and TOKEN not in body


async def test_route_preview_authentication_whitelisted_download_and_current_plan_recheck():
    from train_commute_companion.calendar_export import export_id

    occurrence = replace(calendar_occurrence(), profile_name="PRIVATE PROFILE")
    path = f"/preview/{export_id(occurrence.key)}.html"
    assert path in render(view(occurrence), True)
    async with served(snapshot=view(occurrence)) as (port, calls):
        assert (await request(port, path))[0] == 401 and calls == []
        assert (await request(port, path + "?token=" + TOKEN))[0] == 401 and calls == []
        status, headers, body = await request(port, path, authorization="Bearer " + TOKEN)
        assert status == 200 and calls == [True]
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Content-Disposition"] == 'attachment; filename="2026-10-27.html"'
        assert headers["Cache-Control"] == "no-store"
        assert "PRIVATE PROFILE" not in body and TOKEN not in body and "not live" in body
        assert (await request(port, "/preview/unknown.html", authorization="Bearer " + TOKEN))[
            0
        ] == 404
    skipped = replace(occurrence, not_travelling=True)
    assert path not in render(view(skipped), True)
    async with served(snapshot=view(skipped)) as (port, _):
        assert (await request(port, path, authorization="Bearer " + TOKEN))[0] == 404

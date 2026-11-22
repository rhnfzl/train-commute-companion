import asyncio
import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from app_rig import ONE_PROFILE, occ, view
from runtime_rig import at
from tcc_app.commands import answer
from tcc_app.today import render
from tcc_app.web import health_report, start_web

from train_commute_companion.model import EventKind, SentAlert
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import ActiveDisruption

TOKEN = "example-test-today-token"


async def test_today_refresh_and_manifest_remain_on_the_private_origin():
    profiles, errors = parse_profiles(ONE_PROFILE, None)
    assert not errors
    snapshot = view(occ(), profiles=tuple(profiles))
    async with served(snapshot=snapshot) as (port, calls):
        auth = "Basic " + base64.b64encode(("viewer:" + TOKEN).encode()).decode()
        status, headers, body = await request(port, authorization=auth)
        assert status == 200
        assert '<meta http-equiv="refresh" content="60; url=/today">' in body
        assert '<link rel="manifest" href="/today/manifest.webmanifest">' in body
        assert "manifest-src 'self'" in headers["Content-Security-Policy"]
        status, headers, body = await request(port, "/today/manifest.webmanifest")
        assert status == 200 and calls == [True]
        assert headers["Content-Type"] == "application/manifest+json"
        assert headers["Cache-Control"] == "no-store"
        manifest = json.loads(body)
        assert manifest["start_url"] == manifest["scope"] == "/today"
        assert manifest["display"] == "standalone"
        assert TOKEN not in body and "office" not in body.lower()
        assert all(i["src"].startswith("/today/") for i in manifest["icons"])
        for icon in manifest["icons"]:
            status, headers, png = await request(port, icon["src"])
            assert status == 200 and headers["Content-Type"] == "image/png"
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert (await request(port, manifest["start_url"]))[0] == 401
        assert (await request(port, "/today/paused"))[0] == 401 and calls == [True]
        status, _, body = await request(port, "/today/paused", authorization=auth)
        assert status == 200 and 'http-equiv="refresh"' not in body
        assert 'href="/today/paused">Refresh status' in body


def test_today_is_manual_with_mixed_settings_or_without_profiles():
    profiles, errors = parse_profiles(ONE_PROFILE, None)
    assert not errors
    disabled = replace(profiles[0], alerts=replace(profiles[0].alerts, live_today=False))
    for items in ((), (disabled,), (profiles[0], replace(disabled, id="p2"))):
        body = render(view(occ(), profiles=items), True)
        assert 'http-equiv="refresh"' not in body and 'rel="manifest"' not in body
        assert 'href="/today">Refresh status' in body and "Office A" in body


def test_today_localizes_the_page_and_marks_its_language():
    profiles, errors = parse_profiles(
        ONE_PROFILE.replace('name: "office_a"', 'name: "office_a"\n    alerts: {language: nl}'),
        None,
    )
    assert not errors
    body = render(view(occ(), profiles=tuple(profiles)), True, paused=True)
    assert '<html lang="nl">' in body
    assert "Office A heenreis" in body
    assert '<a href="/today/paused">Status vernieuwen</a>' in body
    assert '<a href="/today">Automatisch vernieuwen hervatten</a>' in body
    assert 'http-equiv="refresh"' not in body


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
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return (
        int(lines[0].split()[1]),
        headers,
        body if headers["Content-Type"] == "image/png" else body.decode(),
    )


@asynccontextmanager
async def served(token=TOKEN, *, snapshot=None, recent_alerts=lambda: (), **settings):
    calls = []

    def today():
        calls.append(True)
        return snapshot if snapshot is not None else view(occ()), True

    server = await start_web(
        0,
        health=lambda: health_report(snapshot or view(occ()), True, True),
        host="127.0.0.1",
        health_token=token,
        today=today,
        recent_alerts=recent_alerts,
        **settings,
    )
    try:
        yield server.sockets[0].getsockname()[1], calls
    finally:
        server.close()
        await server.wait_closed()


async def test_calendar_subscription_has_its_own_token_and_stays_private():
    token = "a" * 64
    calendars = []

    def calendar():
        calendars.append(True)
        return b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"

    async with served(calendar_token=token, calendar=calendar) as (port, calls):
        assert (await request(port, "/calendar/subscription/wrong.ics"))[0] == 404
        assert (await request(port, f"/calendar/subscription/{TOKEN}.ics"))[0] == 404
        assert (await request(port, "/today", f"Bearer {token}"))[0] == 401
        assert calls == calendars == []
        status, headers, body = await request(port, f"/calendar/subscription/{token}.ics")
        assert status == 200 and calendars == [True]
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert body.endswith("END:VCALENDAR\r\n") and token not in body
        status, headers, head = await request(
            port, f"/calendar/subscription/{token}.ics", method="HEAD"
        )
        assert status == 200 and head == "" and int(headers["Content-Length"]) == len(body)
    async with served(calendar=calendar) as (port, _):
        assert (await request(port, f"/calendar/subscription/{token}.ics"))[0] == 404
        assert calendars == [True, True]


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic !!!"])
async def test_today_requires_auth_before_reading_private_view(authorization):
    async with served() as (port, calls):
        status, headers, body = await request(port, authorization=authorization)
        assert status == 401 and calls == []
        assert headers["WWW-Authenticate"] == 'Basic realm="tcc-today"'
        assert body == "Authentication required."
        assert "office" not in body and TOKEN not in body
        assert headers["Cache-Control"] == "no-store"


async def test_only_authenticated_today_reads_and_escapes_alert_history():
    profiles, errors = parse_profiles(ONE_PROFILE, None)
    assert not errors
    profile = profiles[0]
    enabled = replace(profile, alerts=replace(profile.alerts, alert_history=True))
    snapshot = view(occ(), profiles=(enabled,))
    marker = "private-history-marker"
    text = f"{marker}<script>private payload</script>"
    row = SentAlert("row-id", enabled.id, EventKind.CANCELLED, at("07:45"), at("07:46"), text)
    reads = []

    def history():
        reads.append(True)
        return (row,)

    auth = "Bearer " + TOKEN
    async with served(snapshot=snapshot, recent_alerts=history) as (port, calls):
        for path, authorization, expected in (
            ("/today", None, 401),
            ("/today/paused", "Bearer wrong", 401),
            ("/health", None, 200),
            ("/health", auth, 200),
            ("/today/manifest.webmanifest", None, 200),
        ):
            status, _, body = await request(port, path, authorization)
            assert status == expected and marker not in body
        assert reads == calls == []
        for path in ("/today", "/today/paused"):
            status, headers, body = await request(port, path, auth)
            assert status == 200 and headers["Cache-Control"] == "no-store"
            assert "Recent alerts" in body and "07:46" in body
            assert marker in body and text not in body and "row-id" not in body
            assert "&lt;script&gt;private payload&lt;/script&gt;" in body
        assert reads == calls == [True, True]
    assert marker not in json.dumps(answer(snapshot, True, {"tool": "get_status", "profile": "p1"}))
    assert "No recent alerts saved." in render(snapshot, True)
    assert "Recent alerts" not in render(view(occ(), profiles=(profile,)), True, alerts=(row,))


async def test_today_filters_only_visible_history_and_preserves_filters_on_refresh():
    profiles, errors = parse_profiles(ONE_PROFILE, None)
    assert not errors
    first = replace(profiles[0], alerts=replace(profiles[0].alerts, alert_history=True))
    second = replace(first, id="p2", name="Second traveller")
    hidden = replace(first, id="p3", alerts=replace(first.alerts, alert_history=False))
    snapshot = view(occ(), profiles=(first, second, hidden))
    sent = at("07:46")
    rows = (
        SentAlert("1", first.id, EventKind.DELAYED, sent, sent, "target delay"),
        SentAlert(
            "2", first.id, EventKind.DELAYED, sent, sent.replace(day=sent.day - 1), "older delay"
        ),
        SentAlert("3", first.id, EventKind.CANCELLED, sent, sent, "other category"),
        SentAlert("4", second.id, EventKind.DELAYED, sent, sent, "other traveller delay"),
        SentAlert("5", hidden.id, EventKind.DELAYED, sent, sent, "disabled history delay"),
    )
    date = sent.date().isoformat()
    query = f"profile={first.id}&category=delays&from={date}"
    async with served(snapshot=snapshot, recent_alerts=lambda: rows) as (port, calls):
        assert (await request(port, f"/today?{query}"))[0] == 401
        assert calls == []
        auth = "Bearer " + TOKEN
        status, headers, body = await request(port, f"/today?{query}", auth)
        html_query = query.replace("&", "&amp;")
        assert status == 200 and "form-action 'self'" in headers["Content-Security-Policy"]
        assert f'content="60; url=/today?{html_query}"' in body
        assert f'href="/today/paused?{html_query}"' in body
        assert "target delay" in body
        for text in (
            "older delay",
            "other category",
            "other traveller delay",
            "disabled history delay",
        ):
            assert text not in body
        _, _, body = await request(port, "/today/paused?category=platforms", auth)
        assert "No alerts match these filters." in body
        assert 'href="/today?category=platforms"' in body


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
        assert (await request(port, "/today/manifest.webmanifest"))[0] == 404
        assert calls == []
    async with served() as (port, calls):
        status, _, body = await request(port, "/today?token=" + TOKEN)
        assert status == 401 and calls == [] and TOKEN not in body


async def test_token_protected_health_is_not_cacheable():
    async with served() as (port, calls):
        status, headers, _ = await request(port, "/health", authorization="Bearer " + TOKEN)
        assert status == 200 and headers["Cache-Control"] == "no-store" and not calls


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

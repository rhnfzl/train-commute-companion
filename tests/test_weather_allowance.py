from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
import runtime_seam as seam
from builders import make_connection, make_leg
from lifecycle_fixtures import L1, L1X, L2, LATER
from runtime_rig import PROFILE, at, late_usual_world
from test_weather_runtime import forecast_for, weather_rig

from train_commute_companion.calendar import TZ, hhmm
from train_commute_companion.model import AlertSettings, EventKind
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.weather import WeatherHour, WeatherProvider, WeatherView
from train_commute_companion.revisions import occurrence_ctx
from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import decode, encode
from train_commute_companion.weather_allowance import weather_allowance
from train_commute_companion.webhook_send import WebhookPoster


def weather(when=None, *, temp=8, rain=0, snow=0, code=0):
    when = when or at("08:12")
    hour = WeatherHour(when.astimezone(UTC).replace(minute=0), temp, rain, snow, code)
    return WeatherView(
        "AAA", when, hour, when.astimezone(UTC) - timedelta(minutes=30), (), None, None
    )


@pytest.mark.parametrize(
    "values,hazard,minutes",
    [
        ({"rain": 0.2}, "rain", (2, 3, 5)),
        ({"code": 45}, "fog", (2, 3, 5)),
        ({"code": 48}, "fog", (2, 3, 5)),
        ({"snow": 0.1}, "snow", (5, 8, 10)),
        *[({"code": code}, "snow", (5, 8, 10)) for code in (71, 73, 75, 77, 85, 86)],
        *[({"code": code}, "ice", (5, 10, 15)) for code in (56, 57, 66, 67)],
        ({"temp": 1, "rain": 0.2}, "ice", (5, 10, 15)),
        ({"snow": 1, "rain": 2, "code": 45}, "snow", (5, 8, 10)),
        ({"temp": 0, "snow": 1, "rain": 2, "code": 45}, "ice", (5, 10, 15)),
    ],
)
@pytest.mark.parametrize("buffer,band", [(1, 0), (10, 0), (11, 1), (20, 1), (21, 2), (45, 2)])
def test_hazards_boundaries_and_strongest_hazard(values, hazard, minutes, buffer, band):
    view = weather(**values)
    base = at("08:12") - timedelta(minutes=buffer)
    view = replace(view, fetched_at=base - timedelta(minutes=20))
    result = weather_allowance(base, buffer, view, view.fetched_at)
    assert result.hazard == hazard and result.extra_min == minutes[band]
    assert result.suggested_leave_by == base.astimezone(UTC) - timedelta(minutes=minutes[band])
    assert f"+{minutes[band]} min suggested for forecast" in result.text


@pytest.mark.parametrize("values", [{}, {"rain": 0.199}, {"temp": -5}, {"temp": 30}, {"code": 61}])
def test_no_allowance_for_dry_frost_heat_or_below_rain_threshold(values):
    view = weather(**values)
    assert weather_allowance(at("08:02"), 10, view, view.fetched_at) is None


@pytest.mark.parametrize("age", [-1, 0, 3599, 3600])
def test_freshness_and_missing_or_zero_buffer(age):
    view = weather(rain=2)
    now = view.fetched_at + timedelta(seconds=age)
    assert (weather_allowance(at("09:02"), 10, view, now) is not None) is (0 <= age < 3600)
    assert weather_allowance(at("08:12"), 0, view, now) is None
    assert weather_allowance(at("08:02"), 10, replace(view, forecast=None), now) is None
    assert weather_allowance(at("08:02"), 10, replace(view, fetched_at=None), now) is None
    assert (
        weather_allowance(at("08:02"), 10, replace(view, when=view.when + timedelta(hours=2)), now)
        is None
    )


@pytest.mark.parametrize(
    "departure,wanted",
    [
        ("2026-11-22T00:05+01:00", "2026-11-21T22:50+00:00"),
        ("2026-03-29T03:05+02:00", "2026-03-29T00:50+00:00"),
        ("2026-10-25T02:05+01:00", "2026-10-25T00:50+00:00"),
    ],
)
def test_allowance_subtracts_elapsed_minutes_across_midnight_and_dst(departure, wanted):
    dep = datetime.fromisoformat(departure).astimezone(TZ)
    view = weather(dep, snow=1)
    base = dep.astimezone(UTC) - timedelta(minutes=10)
    result = weather_allowance(base, 10, view, view.fetched_at)
    assert result.suggested_leave_by == datetime.fromisoformat(wanted)


@pytest.mark.parametrize("value", [None, "true", "false", "1", "null", '"false"'])
def test_configuration_defaults_validation_and_state_roundtrip(value):
    text = PROFILE
    if value is not None:
        text = text.replace(
            "    name:", f"    alerts: {{weather_allowance: {value}}}\n    name:", 1
        )
    profiles, errors = parse_profiles(text, None)
    if value in ("1", "null", '"false"'):
        assert [e.path for e in errors] == ["profiles[0].alerts.weather_allowance"]
    else:
        assert not errors and profiles[0].alerts.weather_allowance is (value != "false")
        settings = profiles[0].alerts
        assert decode(AlertSettings, encode(settings)) == settings
        assert settings.compensation_reminder is False and settings.quiet_repeat_misses is False
    old = encode(AlertSettings())
    old.pop("weather_allowance")
    assert decode(AlertSettings, old).weather_allowance is True


async def ready(tmp_path, client, *, text=None, now="07:20"):
    rig = weather_rig(tmp_path, None, text)
    rig.clock.set(now)
    rig.rt._weather = WeatherProvider(client, rig.clock.now)
    await rig.rt.start()
    await rig.rt.weather_idle()
    return rig


def shown(rig):
    return next(o for o in rig.rt.view().occurrences if o.key == rig.occ().key)


@pytest.mark.parametrize("off", [None, "weather", "leave_by", "weather_allowance"])
async def test_shared_advice_and_off_switches_do_not_change_journey_or_fetch_on_render(
    tmp_path, off
):
    from tcc_app.commands import connections_tool, render_status

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=forecast_for(at("07:20")))

    text = (
        PROFILE
        if off is None
        else PROFILE.replace("    name:", f"    alerts: {{{off}: false}}\n    name:", 1)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = await ready(tmp_path, client, text=text)
        try:
            state, queries, requests = rig.rt.state, list(rig.provider.calls), len(calls)
            occurrence = shown(rig)
            assert (occurrence.weather_allowance is not None) is (off is None)
            assert (hhmm(occurrence.leave_by) if occurrence.leave_by else None) == (
                None if off == "leave_by" else "08:02"
            )
            status = render_status(rig.rt.view(), True)
            assert ("Leave by 08:00 (+2 min suggested for forecast rain)" in status) is (
                off is None
            )
            tool = connections_tool(rig.rt.view(), True, "p1", occurrence.key.direction)
            assert tool["journeys"][0]["weather_allowance"] == encode(occurrence.weather_allowance)
            assert (
                rig.rt.state == state and rig.provider.calls == queries and len(calls) == requests
            )
            rig.clock.set("07:50")
            await rig.rt.poll_once()
            routine = next(e for e in rig.notifier.attempts if e.kind is EventKind.ALL_GOOD)
            assert ("+2 min suggested for forecast rain" in routine.text) is (off is None)
            assert routine.text.count("Leave by") == (0 if off == "leave_by" else 1)
            assert rig.rt.config.profiles[0].outbound.buffer_min == 10
            assert rig.occ().lock == state.occurrences[next(iter(state.occurrences))].lock
        finally:
            await rig.rt.stop_weather()


@pytest.mark.parametrize("presence", ["at_station", "travelling", "not_travelling"])
async def test_no_allowance_after_arriving_at_station_or_leaving(tmp_path, presence):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=forecast_for(at("07:20")))
        )
    ) as client:
        rig = await ready(tmp_path, client)
        try:
            assert shown(rig).weather_allowance is not None
            rig.occ_set(replace(rig.occ(), **{presence: True}))
            assert shown(rig).weather_allowance is None
        finally:
            await rig.rt.stop_weather()


@pytest.mark.parametrize("malformed", [False, True])
async def test_unknown_malformed_and_stale_data_add_no_allowance(tmp_path, malformed):
    def respond(request):
        raw = forecast_for(at("07:20"))
        if malformed:
            raw["hourly"]["precipitation"][7] = "unknown"
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = await ready(tmp_path, client)
        try:
            assert (shown(rig).weather_allowance is None) is malformed
            rig.clock.set("08:20")
            assert shown(rig).weather_allowance is None
            rig.provider.positions.clear()
            assert shown(rig).weather_allowance is None
        finally:
            await rig.rt.stop_weather()


async def test_selected_train_and_held_messages_use_current_forecast_without_double_text(tmp_path):
    snow = False

    def respond(request):
        raw = forecast_for(at("07:20"))
        if snow:
            raw["hourly"].update(temperature_2m=[3] * 24, snowfall=[1] * 24, weather_code=[71] * 24)
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = await ready(tmp_path, client)
        try:
            rig.notifier.fails = lambda event: event.kind is EventKind.ALL_GOOD
            rig.clock.set("07:50")
            await rig.rt.poll_once()
            assert any("Leave by 08:00 (+2 min" in e.text for e in rig.notifier.attempts)
            snow = True
            rig.provider.positions["AAA"] = (52.11, 5.1)
            early = replace(
                L1,
                stops=(
                    replace(
                        L1.board,
                        actual_track="5",
                        departure=replace(L1.board.departure, value=at("08:10")),
                    ),
                    *L1.stops[1:],
                ),
            )
            rig.provider.world = [make_connection([early, L2]), LATER]
            rig.clock.set("07:52")
            await rig.rt.poll_once()
            await rig.rt.weather_idle()
            rig.notifier.fails = lambda event: False
            await rig.rt.watchdog_once()
            delivered = next(e for e in rig.rt.events_log if e.kind is EventKind.ALL_GOOD)
            assert "Leave by 07:55 (+5 min suggested for forecast snow)" in delivered.text
            assert delivered.text.count("Leave by") == 1 and "+2 min" not in delivered.text
            assert "platform" in delivered.text and "on time per NS" in delivered.text
            assert "https://open-meteo.com/" in delivered.text
            assert await rig.rt.choose_at("p1", time(8, 25)) is None
            selected = shown(rig)
            assert hhmm(selected.leave_by) == "08:15"
            assert hhmm(selected.weather_allowance.suggested_leave_by) == "08:10"
        finally:
            await rig.rt.stop_weather()


@pytest.mark.parametrize("off", ["weather", "leave_by", "weather_allowance"])
async def test_held_advice_obeys_new_off_setting(tmp_path, off):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=forecast_for(at("07:20")))
        )
    ) as client:
        rig = await ready(tmp_path, client)
        try:
            rig.notifier.fails = lambda event: event.kind is EventKind.ALL_GOOD
            rig.clock.set("07:50")
            await rig.rt.poll_once()
            rig.write(
                text=PROFILE.replace("    name:", f"    alerts: {{{off}: false}}\n    name:", 1)
            )
            await rig.rt.reload_if_changed()
            rig.notifier.fails = lambda event: False
            await rig.rt.watchdog_once()
            delivered = next(e for e in rig.rt.events_log if e.kind is EventKind.ALL_GOOD)
            assert "suggested" not in delivered.text
            assert ("Leave by 08:02" in delivered.text) is (off != "leave_by")
        finally:
            await rig.rt.stop_weather()


async def test_allowance_keeps_planner_buffers_deadlines_baselines_and_timers_identical(tmp_path):
    rigs = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=forecast_for(at("07:20")))
        )
    ) as client:
        try:
            for enabled in (True, False):
                folder = tmp_path / str(enabled)
                folder.mkdir()
                text = PROFILE.replace("NAME", "office_a").replace(
                    "    name:",
                    f"    alerts: {{weather_allowance: {str(enabled).lower()}}}\n    name:",
                    1,
                )
                rigs.append(await ready(folder, client, text=text))
            for stamp in ("07:50", "07:58", "08:02"):
                for rig in rigs:
                    rig.clock.set(stamp)
                    await rig.rt.poll_once()
                    await rig.rt.weather_idle()
                a, b = rigs
                assert a.provider.calls == b.provider.calls
                assert a.rt.state.occurrences == b.rt.state.occurrences
                assert a.rt.state.baselines == b.rt.state.baselines
                assert a.rt.state.journey_records == b.rt.state.journey_records
                assert a.rt.config.profiles[0].outbound == b.rt.config.profiles[0].outbound
                assert occurrence_ctx(a.rt.state, a.occ(), a.clock.now()).schedule == (
                    occurrence_ctx(b.rt.state, b.occ(), b.clock.now()).schedule
                )
                assert [(e.key, e.train, e.severity) for e in a.rt.events_log] == [
                    (e.key, e.train, e.severity) for e in b.rt.events_log
                ]
        finally:
            for rig in rigs:
                await rig.rt.stop_weather()


async def test_arrival_hazard_does_not_add_departure_allowance(tmp_path):
    def respond(request):
        raw = forecast_for(at("07:20"))
        raw["hourly"]["precipitation"] = [0] * 24
        if float(request.url.params["latitude"]) < 52:
            raw["hourly"]["snowfall"] = [1] * 24
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = await ready(tmp_path, client)
        try:
            assert shown(rig).weather[-1].forecast.snowfall_cm == 1
            assert shown(rig).weather_allowance is None
        finally:
            await rig.rt.stop_weather()


async def test_held_cancellation_updates_switched_replacement_advice_once(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=forecast_for(at("07:20")))
        )
    ) as client:
        rig = await ready(tmp_path, client)
        try:
            rig.clock.set("07:50")
            await rig.rt.poll_once()
            rig.notifier.fails = lambda event: event.kind is EventKind.CANCELLED
            rig.provider.world = [make_connection([L1X, L2]), LATER]
            rig.clock.set("07:52")
            await rig.rt.poll_once()
            held = next(e for e in reversed(rig.notifier.attempts) if e.kind is EventKind.CANCELLED)
            assert "Leave by 08:13 (+2 min" in held.text
            first = make_leg("AAA", "UT", "08:44", "09:12", number="4321")
            onward = make_leg("UT", "RTD", "09:22", "09:56", number="9999")
            rig.provider.world = [make_connection([L1X, L2]), make_connection([first, onward])]
            assert await rig.rt.choose_at("p1", time(8, 44)) is None
            rig.clock.set("07:54")
            await rig.rt.poll_once()
            rig.notifier.fails = lambda event: False
            await rig.rt.watchdog_once()
            event = next(e for e in rig.rt.events_log if e.kind is EventKind.CANCELLED)
            assert "Leave by 08:32 (+2 min" in event.text
            assert "Leave by 08:13" not in event.text and event.text.count("Leave by") == 1
        finally:
            await rig.rt.stop_weather()


async def test_earlier_advice_uses_its_own_hour_and_the_return_buffer(tmp_path):
    def respond(request):
        raw = forecast_for(at("16:54"))
        raw["hourly"].update(precipitation=[0] * 24, weather_code=[0] * 24)
        raw["hourly"]["snowfall"][16] = 1  # 17:22 local, distinct from the usual 17:52 hour
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = weather_rig(tmp_path, None)
        rig.clock.set("16:54")
        rig.provider.world = late_usual_world()
        rig.notifier.fails = lambda event: event.kind is EventKind.LEAVE_NOW
        rig.rt._weather = WeatherProvider(client, rig.clock.now)
        await rig.rt.start()
        try:
            await rig.rt.weather_idle()
            rig.notifier.fails = lambda event: False
            await rig.rt.watchdog_once()
            event = next(e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW)
            assert "Leave by 17:12 (+5 min suggested for forecast snow)" in event.text
            assert event.text.count("Leave by") == 1
            assert "forecast hour 17:00" in event.text and "https://open-meteo.com/" in event.text
            assert rig.occ("p1|return|2026-10-27").lock.legs[0].train.train_number == "7952"
        finally:
            await rig.rt.stop_weather()


@pytest.mark.parametrize("now,extra", [("17:12", True), ("17:15", False)])
async def test_retry_hides_only_elapsed_weather_suggestion(tmp_path, now, extra):
    def respond(request):
        raw = forecast_for(at("16:54"))
        raw["hourly"].update(precipitation=[0] * 24, snowfall=[1] * 24, weather_code=[71] * 24)
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = weather_rig(tmp_path, None)
        rig.clock.set("16:54")
        rig.provider.world = late_usual_world()
        rig.notifier.fails = lambda event: event.kind is EventKind.LEAVE_NOW
        rig.rt._weather = WeatherProvider(client, rig.clock.now)
        await rig.rt.start()
        try:
            await rig.rt.weather_idle()
            rig.clock.set(now)
            rig.notifier.fails = lambda event: False
            await rig.rt.watchdog_once()
            event = next(e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW)
            assert ("+5 min suggested" in event.text) is extra
            assert ("Leave by 17:12" if extra else "Leave by 17:17") in event.text
            assert event.text.count("Leave by") == 1
        finally:
            await rig.rt.stop_weather()


async def test_earlier_retry_uses_the_same_latest_advice_connection_for_weather(tmp_path):
    def respond(request):
        raw = forecast_for(at("16:54"))
        raw["hourly"].update(precipitation=[0] * 24, weather_code=[0] * 24)
        raw["hourly"]["snowfall"][16] = 1
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = weather_rig(tmp_path, None)
        rig.clock.set("16:54")
        rig.provider.world = late_usual_world()
        rig.notifier.fails = lambda event: event.kind is EventKind.LEAVE_NOW
        rig.rt._weather = WeatherProvider(client, rig.clock.now)
        await rig.rt.start()
        try:
            await rig.rt.weather_idle()
            key = "p1|return|2026-10-27"
            answers = seam.memory(rig.rt).boards[key]
            assert len(answers) >= 2
            updated = []
            for connection in answers[-1].connections:
                first = connection.legs[0]
                if first.train.train_number == "7922":
                    board = replace(
                        first.board, departure=replace(first.board.departure, value=at("17:34"))
                    )
                    connection = replace(
                        connection, legs=(replace(first, stops=(board, *first.stops[1:])),)
                    )
                updated.append(connection)
            seam.memory(rig.rt).boards[key] = (
                *answers[:-1],
                replace(
                    answers[-1],
                    connections=tuple(updated),
                    fetched_at=answers[-1].fetched_at + timedelta(seconds=1),
                ),
            )
            rig.clock.set("16:56")
            rig.notifier.fails = lambda event: False
            await rig.rt.watchdog_once()
            event = next(e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW)
            assert "17:34" in event.text
            assert "Leave by 17:17" in event.text
            assert "+5 min suggested" not in event.text
        finally:
            await rig.rt.stop_weather()


async def test_phone_and_webhook_share_weather_departure_text(tmp_path):
    copies = []

    def respond(request):
        if request.url.host == "copy.example.test":
            import json

            copies.append(json.loads(request.content))
            return httpx.Response(200)
        raw = forecast_for(at("07:20"))
        raw["hourly"].update(precipitation=[0] * 24, snowfall=[1] * 24, weather_code=[71] * 24)
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig = weather_rig(tmp_path, None)
        rig.clock.set("07:20")
        rig.rt = Runtime(
            profile_path=rig.path,
            store=rig.store,
            provider=rig.provider,
            notifier=rig.notifier,
            clock=rig.clock,
            require_stations=False,
            weather=WeatherProvider(client, rig.clock.now),
            webhooks=WebhookPoster(
                client, ("https://copy.example.test/hook",), "test-webhook-secret", rig.clock.now
            ),
        )
        await rig.rt.start()
        try:
            await rig.rt.weather_idle()
            rig.clock.set("07:50")
            await rig.rt.poll_once()
            await rig.rt.webhooks_idle()
            phone = next(e for e in rig.rt.events_log if e.kind is EventKind.ALL_GOOD)
            copy = next(e for e in copies if e["kind"] == "all_good")
            assert "Leave by 07:57 (+5 min suggested" in phone.text
            assert copy["text"] == phone.text
        finally:
            await rig.rt.stop_weather()
            await seam.stop_webhooks(rig.rt)

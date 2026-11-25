import asyncio
from dataclasses import replace
from datetime import UTC, timedelta

import httpx
import pytest
from builders import cut_stop
from recovery_sample import NOW as RECOVERY_NOW
from recovery_sample import answer as recovery_answer
from runtime_rig import PROFILE, Rig
from test_runtime_recovery import PROFILE as RECOVERY_PROFILE
from test_runtime_recovery import follow, occurrence

from train_commute_companion.model import EventKind, Stations
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.weather import WeatherProvider
from train_commute_companion.runtime import Runtime


def forecast_for(now):
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°C",
            "precipitation": "mm",
            "snowfall": "cm",
            "weather_code": "wmo code",
        },
        "hourly": {
            "time": [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24)],
            "temperature_2m": [8] * 24,
            "precipitation": [2] * 24,
            "snowfall": [0] * 24,
            "weather_code": [61] * 24,
        },
    }


def weather_rig(tmp_path, provider, text=None):
    rig = Rig(tmp_path)
    if text is not None:
        rig.write(text=text)
    rig.provider.positions = {"AAA": (52.1, 5.1), "RTD": (51.9, 4.5)}
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        weather=provider,
    )
    return rig


@pytest.mark.parametrize("value", [None, True, False, "yes", 1])
def test_weather_switch_is_default_on_and_accepts_only_booleans(value):
    text = PROFILE
    if value is not None:
        text = text.replace(
            '    name: "NAME"', f'    name: "NAME"\n    alerts: {{weather: {str(value).lower()}}}'
        )
    profiles, errors = parse_profiles(text, None)
    if value in ("yes", 1) and type(value) is not bool:
        # YAML's unquoted yes is a boolean, so quote it to test a string.
        if value == "yes":
            text = text.replace("weather: yes", 'weather: "yes"')
            profiles, errors = parse_profiles(text, None)
        assert errors
    else:
        assert not errors and profiles[0].alerts.weather is (value is not False)


@pytest.mark.parametrize("enabled", [False, True])
def test_weather_and_delay_thresholds_keep_their_own_values(enabled):
    text = PROFILE.replace(
        '    name: "NAME"',
        '    name: "NAME"\n    alerts: {weather: '
        + str(enabled).lower()
        + ", delay_min: 10, delay_step_min: 5}",
    )
    profiles, errors = parse_profiles(text, None)
    assert not errors
    assert profiles[0].alerts.weather is enabled
    assert profiles[0].alerts.delay_min == 10
    assert profiles[0].alerts.delay_step_min == 5


async def test_weather_disabled_does_no_profile_network_work_and_shows_no_weather(tmp_path):
    async def unexpected(request):
        pytest.fail("disabled weather made a network request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        provider = WeatherProvider(client, lambda: None)
        rig = weather_rig(
            tmp_path,
            provider,
            PROFILE.replace('    name: "NAME"', '    name: "NAME"\n    alerts: {weather: false}'),
        )
        await rig.rt.start()
        await rig.rt.weather_idle()
        assert all(o.weather == () for o in rig.rt.view().occurrences)
        assert all("Open-Meteo" not in e.text for e in rig.notifier.attempts)
        await rig.rt.stop_weather()


async def test_hanging_weather_never_blocks_cancellation_watchdog_or_storage(tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def hanging(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(hanging)) as client:
        rig = weather_rig(tmp_path, None)
        provider = WeatherProvider(client, rig.clock.now)
        rig.rt = Runtime(
            profile_path=rig.path,
            store=rig.store,
            provider=rig.provider,
            notifier=rig.notifier,
            clock=rig.clock,
            require_stations=False,
            weather=provider,
        )
        await rig.rt.start()
        await asyncio.wait_for(entered.wait(), 1)
        locked = rig.occ().lock
        original = rig.provider.world[0]
        rig.clock.jump(2)
        rig.provider.world = [
            replace(
                original,
                legs=(
                    replace(
                        original.legs[0],
                        cancelled=True,
                        stops=tuple(cut_stop(stop) for stop in original.legs[0].stops),
                    ),
                    *original.legs[1:],
                ),
            )
        ]
        try:
            await asyncio.wait_for(rig.rt.poll_once(), 1)
            assert any(e.kind is EventKind.CANCELLED for e in rig.notifier.attempts)
            assert rig.occ().lock.legs[0].train == locked.legs[0].train
            rig.clock.jump(10)
            rig.store.fail = True
            await asyncio.wait_for(rig.rt.watchdog_once(), 1)
            assert not rig.rt.store_ok
            assert any(e.kind is EventKind.STORAGE_FAILED for e in rig.notifier.attempts)
        finally:
            await asyncio.wait_for(rig.rt.stop_weather(), 1)
        assert cancelled.is_set()


async def test_endpoint_times_tips_freshness_and_switch_edits_are_shared_by_consumers(tmp_path):
    from tcc_app.commands import render_status

    rig = weather_rig(tmp_path, None)
    rig.clock.set("07:20")
    calls = []

    def respond(request):
        calls.append(request)
        forecast = forecast_for(rig.clock.now())
        forecast["hourly"].update(precipitation=[0] * 24, weather_code=[0] * 24)
        if request.url.params["latitude"] == "51.9":
            forecast["hourly"]["temperature_2m"] = [0] * 24
        return httpx.Response(200, json=forecast)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig.rt = Runtime(
            profile_path=rig.path,
            store=rig.store,
            provider=rig.provider,
            notifier=rig.notifier,
            clock=rig.clock,
            require_stations=False,
            known_stations=Stations(
                {"AAA": ["Alpha"], "RTD": ["Rotterdam Centraal"], "UT": ["Utrecht"]}
            ),
            weather=WeatherProvider(client, rig.clock.now),
        )
        await rig.rt.start()
        original = rig.occ().lock
        await rig.rt.weather_idle()
        view = rig.rt.view()
        endpoint = view.occurrences[0].weather
        assert [(w.station, w.when.strftime("%H:%M")) for w in endpoint] == [
            ("AAA", "08:12"),
            ("RTD", "09:24"),
        ]
        assert all(w.forecast is not None for w in endpoint)
        assert len(calls) == 2  # transfer station UT is not an endpoint
        status = render_status(view, True)
        assert "Frost possible" in status
        assert status.count("Retrieved") == status.count("KNMI warnings unavailable") == 2
        rig.clock.set("07:50")
        await rig.rt.poll_once()
        await rig.rt.weather_idle()
        routine = [e for e in rig.notifier.attempts if e.kind is EventKind.ALL_GOOD]
        assert routine
        train, weather = routine[-1].text.splitlines()
        assert "on time per NS" in train and "Leave by 08:02." in train
        assert weather == "Rotterdam Centraal: ❄️ Frost possible. Watch your step."
        assert rig.occ().lock == original and len(calls) == 2
        assert "52.1" not in rig.store.text and "temperature_c" not in rig.store.text
        rig.clock.jump(31)
        assert all(w.forecast is None for w in rig.rt.view().occurrences[0].weather)
        rig.write(
            text=PROFILE.replace(
                '    name: "NAME"', '    name: "NAME"\n    alerts: {weather: false}'
            )
        )
        await rig.rt.reload_if_changed()
        await rig.rt.poll_once()
        await rig.rt.weather_idle()
        assert rig.rt.view().occurrences[0].weather == () and len(calls) == 2
        assert "Open-Meteo" not in render_status(rig.rt.view(), True)
        await rig.rt.stop_weather()


async def test_new_and_still_running_removed_profiles_share_station_forecasts(tmp_path):
    rig = weather_rig(tmp_path, None)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=forecast_for(rig.clock.now()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig.rt = Runtime(
            profile_path=rig.path,
            store=rig.store,
            provider=rig.provider,
            notifier=rig.notifier,
            clock=rig.clock,
            require_stations=False,
            weather=WeatherProvider(client, rig.clock.now),
        )
        await rig.rt.start()
        await rig.rt.weather_idle()
        # Removed profiles keep following their current train while the new profile starts.
        text = PROFILE[: PROFILE.index('  - id: "p2"')].replace('id: "p1"', 'id: "new"')
        rig.write(text=text)
        await rig.rt.reload_if_changed()
        await rig.rt.poll_once()
        await rig.rt.weather_idle()
        active = [o for o in rig.rt.view().occurrences if o.weather]
        assert {o.key.profile_id for o in active} == {"p1", "new"}
        assert len(calls) == 2
        assert all(len(o.weather) == 2 for o in active)
        await rig.rt.stop_weather()


async def test_selected_recovery_weather_uses_the_accepted_itinerary(tmp_path):
    rig = weather_rig(tmp_path, None, RECOVERY_PROFILE)
    rig.clock.t = RECOVERY_NOW
    rig.provider.world = list(recovery_answer().connections)
    rig.provider.positions = {"UT": (52.1, 5.1), "GD": (52.0, 4.7), "RTD": (51.9, 4.5)}

    def respond(request):
        return httpx.Response(200, json=forecast_for(rig.clock.now()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rig.rt._weather = WeatherProvider(client, rig.clock.now)
        await rig.rt.start()
        try:
            await rig.rt.weather_idle()
            selected = await follow(rig)
            await rig.rt.poll_once()
            await rig.rt.weather_idle()
            shown = occurrence(rig)
            assert shown.weather_allowance is None
            steps = selected.itinerary.steps
            expected = (
                (steps[0].origin.station_code, steps[0].departure),
                (steps[-1].destination.station_code, steps[-1].arrival),
            )
            assert Runtime._weather_points(shown) == expected
            assert [(w.station, w.when) for w in shown.weather] == list(expected)
            assert all(w.forecast is not None for w in shown.weather)
            assert Runtime._weather_points(replace(shown, legs=())) == expected
            assert Runtime._weather_points(replace(shown, resolved=True)) == expected
            assert Runtime._weather_points(replace(shown, not_travelling=True)) == ()
            rig.provider.positions.pop(steps[0].origin.station_code)
            assert [(w.station, w.when) for w in occurrence(rig).weather] == [expected[-1]]
        finally:
            await rig.rt.stop_weather()
            await rig.rt._recovery.stop()

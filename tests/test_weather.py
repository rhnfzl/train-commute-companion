from datetime import UTC, datetime, timedelta

import httpx
import pytest

from train_commute_companion.providers.weather import (
    WeatherProvider,
    parse_forecast,
    parse_warnings,
    weather_text,
)

NOW = datetime(2026, 11, 21, 7, tzinfo=UTC)
AT = NOW + timedelta(hours=1, minutes=12)
POSITION = (52.1, 5.1)


def forecast(temp=8, rain=2, snow=0, code=61):
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
            "time": ["2026-11-21T08:00", "2026-11-21T09:00"],
            "temperature_2m": [temp, temp],
            "precipitation": [rain, rain],
            "snowfall": [snow, snow],
            "weather_code": [code, code],
        },
    }


def warnings(color="yellow"):
    return {
        "metadata": {
            "generated_at": NOW.isoformat(),
            "schema_version": "0.7",
            "publication_type": "manual",
            "domain": "public",
            "producer": {"name": "KNMI"},
            "snapshot_uuid": "a1",
        },
        "num_warning_sets": 1,
        "num_warnings": 1,
        "warnings": [
            {
                "warning_set_uuid": "s1",
                "version_id": 1,
                "version_dt": NOW.isoformat(),
                "operational_status": "Actual",
                "domain": "public",
                "status": "active",
                "warning_type": "warning",
                "phenomenon": "road-ice",
                "incident": {},
                "onset": NOW.isoformat(),
                "expires": (NOW + timedelta(hours=3)).isoformat(),
                "texts": {"nl": {"headline": "Gladheid", "description": "Kans op gladheid."}},
                "parts": [
                    {
                        "part_uuid": "p1",
                        "version_id": 1,
                        "version_dt": NOW.isoformat(),
                        "color": color,
                        "severity": {"yellow": "Moderate", "orange": "Severe", "red": "Extreme"}[
                            color
                        ],
                        "probability_percentage_range": "60-90",
                        "status": "active",
                        "onset": NOW.isoformat(),
                        "expires": (NOW + timedelta(hours=3)).isoformat(),
                        "area": {
                            "type": "Polygon",
                            "coordinates": [[[5, 52], [6, 52], [6, 53], [5, 53], [5, 52]]],
                        },
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "temp,rain,snow,code,tip",
    [
        (8, 2, 0, 61, "rain"),
        (0, 0, 1, 71, "snow"),
        (31, 0, 0, 0, "water"),
        (-2, 0, 0, 0, "frost"),
        (0, 1, 0, 66, "ice"),
    ],
)
def test_forecast_tips_are_derived_from_reported_values(temp, rain, snow, code, tip):
    parsed = parse_forecast(forecast(temp, rain, snow, code), NOW)
    assert tip in " ".join(parsed.hours[0].tips).lower()
    assert parsed.fetched_at == NOW


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x["hourly"]["temperature_2m"].__setitem__(0, None),
        lambda x: x["hourly"]["precipitation"].clear(),
        lambda x: x["hourly"]["snowfall"].__setitem__(0, -1),
        lambda x: x["hourly"]["temperature_2m"].__setitem__(0, float("nan")),
        lambda x: x["hourly_units"].__setitem__("temperature_2m", "°F"),
        lambda x: x.__setitem__("utc_offset_seconds", 3600),
    ],
)
def test_malformed_forecast_is_unavailable(change):
    raw = forecast()
    change(raw)
    with pytest.raises((ValueError, TypeError)):
        parse_forecast(raw, NOW)


@pytest.mark.parametrize("color", ["yellow", "orange", "red"])
def test_official_warnings_match_station_geometry_and_commute_time(color):
    parsed = parse_warnings(warnings(color), NOW)
    assert [w.color for w in parsed.at(POSITION, AT)] == [color]
    assert not parsed.at((53.5, 6.5), AT)
    assert not parsed.at(POSITION, NOW - timedelta(seconds=1))
    assert not parsed.at(POSITION, NOW + timedelta(hours=3))
    assert parsed.at((52, 5), AT)  # include the polygon boundary


def test_multipolygon_and_severity_without_enrichment():
    raw = warnings("orange")
    part = raw["warnings"][0]["parts"][0]
    del part["color"]
    part["area"] = {"type": "MultiPolygon", "coordinates": [part["area"]["coordinates"]]}
    assert parse_warnings(raw, NOW).at(POSITION, AT)[0].color == "orange"


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x["metadata"].__setitem__(
            "generated_at", (NOW - timedelta(hours=27)).isoformat()
        ),
        lambda x: x["metadata"].__setitem__("generated_at", (NOW + timedelta(hours=1)).isoformat()),
        lambda x: x["metadata"].__setitem__("schema_version", "unknown"),
        lambda x: x["metadata"].__setitem__("publication_type", "test"),
        lambda x: x["warnings"][0].__setitem__("operational_status", "Unknown"),
        lambda x: x["warnings"][0].__setitem__("operational_status", None),
        lambda x: x["warnings"][0].pop("operational_status"),
        lambda x: x.__setitem__("num_warnings", 2),
        lambda x: x["warnings"][0]["parts"][0].__setitem__("severity", "unknown"),
        lambda x: x["warnings"][0]["parts"][0]["area"].__setitem__("coordinates", []),
    ],
)
def test_unknown_warning_source_never_becomes_no_warnings(change):
    raw = warnings()
    change(raw)
    with pytest.raises((ValueError, TypeError)):
        parse_warnings(raw, NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "cancelled"),
        ("status", "expired"),
        ("operational_status", "Test"),
        ("operational_status", "Exercise"),
        ("operational_status", "System"),
        ("warning_type", "potentially-dangerous-weather"),
    ],
)
def test_nonoperational_or_cancelled_warnings_are_not_shown(field, value):
    raw = warnings()
    raw["warnings"][0][field] = value
    assert not parse_warnings(raw, NOW).at(POSITION, AT)


async def test_cache_expires_refreshes_and_never_sends_knmi_key_to_forecast_or_download():
    now = NOW
    calls = []
    broken = False

    def respond(request):
        calls.append(request)
        host = request.url.host
        if host == "api.dataplatform.knmi.nl":
            assert request.headers["Authorization"] == "user-test-key"
            if request.url.path.endswith("/url"):
                return httpx.Response(
                    200,
                    json={
                        "temporaryDownloadUrl": "https://objectstore.dataplatform.knmi.nl/data.json"
                    },
                )
            return httpx.Response(200, json={"files": [{"filename": "snapshot.json"}]})
        assert "Authorization" not in request.headers
        if host == "api.open-meteo.com":
            assert request.url.params["models"] == "knmi_seamless"
            assert request.url.params["latitude"] == "52.1"
            if broken:
                return httpx.Response(503)
            return httpx.Response(200, json=forecast())
        return httpx.Response(200, json=warnings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WeatherProvider(client, lambda: now, "user-test-key")
        await provider.refresh({"AAA": POSITION})
        view = provider.snapshot("AAA", POSITION, AT, now)
        assert view.forecast.temperature_c == 8
        assert view.warnings[0].color == "yellow"
        text = weather_text(view)
        assert "https://open-meteo.com/" in text and "KNMI" in text
        assert "2026-11-21" in text and "CC BY 4.0" in text
        assert "adapted" in text
        await provider.refresh({"AAA": POSITION})
        assert len(calls) == 4
        now += timedelta(hours=1)
        assert provider.snapshot("AAA", POSITION, AT, now).forecast is None
        assert provider.snapshot("AAA", POSITION, AT, now).warnings_checked_at is None
        broken = True
        await provider.refresh({"AAA": POSITION})
        assert provider.snapshot("AAA", POSITION, AT, now).forecast is None
        assert sum(r.url.host == "api.open-meteo.com" for r in calls) == 2


async def test_missing_key_and_bad_warning_source_are_explicitly_unknown():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=forecast()))
    ) as client:
        provider = WeatherProvider(client, lambda: NOW)
        await provider.refresh({"AAA": POSITION})
        view = provider.snapshot("AAA", POSITION, AT, NOW)
        assert view.warnings_checked_at is None and view.warnings == ()
        assert "warnings unavailable" in weather_text(view)


async def test_station_cache_is_bounded_and_old_station_coordinates_are_not_reused():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WeatherProvider(client, lambda: NOW)
        await provider.refresh({str(i): POSITION for i in range(100)})
        assert len(calls) <= 32
        await provider.refresh({"new": POSITION})
        assert provider.snapshot("0", POSITION, AT, NOW).forecast is None
        assert provider.snapshot("new", (53, 6), AT, NOW).forecast is None


async def test_download_redirect_does_not_forward_credentials():
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=forecast())
        return httpx.Response(302, headers={"Location": "https://example.org/steal"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        provider = WeatherProvider(client, lambda: NOW, "user-test-key")
        await provider.refresh({"AAA": POSITION})
        assert all(r.url.host != "example.org" for r in calls)
        assert provider.snapshot("AAA", POSITION, AT, NOW).warnings_checked_at is None


async def test_failed_warning_download_does_not_claim_clear_weather():
    def respond(request):
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=forecast())
        if request.url.path.endswith("/url"):
            return httpx.Response(
                200,
                json={"temporaryDownloadUrl": "https://objectstore.dataplatform.knmi.nl/data.json"},
            )
        if request.url.host == "api.dataplatform.knmi.nl":
            return httpx.Response(200, json={"files": [{"filename": "snapshot.json"}]})
        return httpx.Response(200, json={"unexpected": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WeatherProvider(client, lambda: NOW, "user-test-key")
        await provider.refresh({"AAA": POSITION})
        view = provider.snapshot("AAA", POSITION, AT, NOW)
        assert view.forecast is not None
        assert "warnings unavailable" in weather_text(view)


def test_empty_official_snapshot_proves_no_published_warning():
    raw = warnings()
    raw.update(warnings=[], num_warning_sets=0, num_warnings=0)
    assert parse_warnings(raw, NOW).at(POSITION, AT) == ()


async def test_failure_retries_are_spaced_and_removed_stations_are_not_fetched():
    now = NOW
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WeatherProvider(client, lambda: now)
        await provider.refresh({"AAA": POSITION})
        for _ in range(4):
            now += timedelta(minutes=1)
            await provider.refresh({"AAA": POSITION})
        assert len(calls) == 1
        now += timedelta(minutes=1)
        await provider.refresh({"AAA": POSITION})
        assert len(calls) == 2
        await provider.refresh({})
        assert provider.snapshot("AAA", POSITION, AT, now).forecast is None


@pytest.mark.parametrize(
    "link",
    [
        "http://objectstore.dataplatform.knmi.nl/data.json",
        "https://example.org/data.json",
        "https://127.0.0.1/data.json",
        "https://api.dataplatform.knmi.nl@evil.example/data.json",
    ],
)
async def test_invalid_download_destinations_are_never_requested(link):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=forecast())
        if request.url.path.endswith("/url"):
            return httpx.Response(200, json={"temporaryDownloadUrl": link})
        return httpx.Response(200, json={"files": [{"filename": "snapshot.json"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WeatherProvider(client, lambda: NOW, "user-test-key")
        await provider.refresh({"AAA": POSITION})
        assert len(calls) == 3
        assert provider.snapshot("AAA", POSITION, AT, NOW).warnings_checked_at is None

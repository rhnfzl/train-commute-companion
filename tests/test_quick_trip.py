import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from builders import make_connection, make_leg
from runtime_rig import DAY, PROFILE, Rig

from train_commute_companion.messages import RefusalKind, Refused
from train_commute_companion.model import Provenance, Stations
from train_commute_companion.profile import load_config
from train_commute_companion.providers.base import AuthError, RateLimited
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.runtime import Runtime

CATALOGUE = Stations(
    {"AAA": ("Alpha Central",), "RTD": ("Rotterdam Centraal",), "UT": ("Utrecht Centraal",)}
)


async def test_unconfigured_quick_trip_reports_key_rejection(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.path.unlink()
    await rig.rt.start()

    async def reject():
        raise AuthError("rejected")

    monkeypatch.setattr(rig.provider, "stations", reject)
    result = await rig.rt.check_trip("AAA", "RTD")
    assert isinstance(result, Refused) and "rejected your API key" in result.text
    assert rig.rt.key_rejected


@pytest.mark.parametrize("value", ['"false"', "1", "null"])
def test_quick_trip_switch_requires_a_boolean(value):
    text = PROFILE.replace(
        '    name: "office_b"', f'    name: "office_b"\n    alerts: {{quick_trip: {value}}}'
    )
    _, errors = load_config(text, None, None, DAY)
    assert [e.path for e in errors] == ["profiles[1].alerts.quick_trip"]


async def test_quick_trip_uses_the_ns_station_catalogue_and_trips(tmp_path):
    rig = Rig(tmp_path)
    rig.path.unlink()
    fixture = Path(__file__).parent / "data/ns/on_time_direct.json"
    payload = json.loads(fixture.read_text())
    payload["trips"][0]["legs"][0]["crowdForecast"] = "LOW"
    paths = []

    def ns(request):
        paths.append(request.url.path.rsplit("/", 2)[-2:])
        if request.url.path.endswith("/v2/stations"):
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {"code": "AAA", "namen": {"lang": "Alpha Central"}},
                        {"code": "BBB", "namen": {"lang": "Beta Central"}},
                    ]
                },
            )
        assert request.url.path.endswith("/v3/trips")
        assert request.url.params["fromStation"] == "AAA"
        assert request.url.params["toStation"] == "BBB"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ns)) as client:
        rt = Runtime(
            profile_path=rig.path,
            store=rig.store,
            notifier=rig.notifier,
            clock=rig.clock,
            provider=NsProvider("mock-key", client, rig.clock.now),
            require_stations=True,
        )
        await rt.start()
        result = await rt.check_trip("Alpha Central", "bbb")

    assert isinstance(result, dict)
    leg = result["options"][0]["legs"][0]
    assert leg["platform"] == {"scheduled": "1", "reported": "1"}
    assert leg["crowd_forecast"] == "low"
    assert paths == [["v2", "stations"], ["v3", "trips"]]


async def test_quick_trip_without_profile_returns_up_to_three_upcoming_options(tmp_path):
    rig = Rig(tmp_path)
    rig.path.unlink()
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    planned = make_leg("AAA", "RTD", "08:10", "09:01", provenance=Provenance.PLANNED)
    reported = make_leg("AAA", "RTD", "07:48", "08:54", delay_dep=12, number="2000")
    reported = replace(
        reported,
        stops=(replace(reported.board, actual_track="5", planned_track="4"), reported.alight),
        crowd_forecast="high",
    )
    rig.provider.world = [
        make_connection([make_leg("AAA", "RTD", "07:49", "08:51")]),
        make_connection([make_leg("AAA", "RTD", "08:20", "09:09", number="1000")]),
        make_connection([make_leg("AAA", "RTD", "08:30", "09:21")]),
        make_connection([reported]),
        make_connection([reported]),
        make_connection([planned]),
        make_connection([make_leg("AAA", "RTD", "07:55", "08:55")], status="CANCELLED"),
        make_connection([replace(make_leg("AAA", "RTD", "08:01", "09:00"), reachable=False)]),
    ]
    before = rig.rt.state, rig.store.writes, list(rig.rt.events_log)

    result = await rig.rt.check_trip("Alpha Central", "rtd")

    assert isinstance(result, dict)
    assert result["status"] == "ok"
    assert [option["legs"][0]["train_number"] for option in result["options"]] == [
        "2000",
        "1234",
        "1000",
    ]
    first, second, _ = [option["legs"][0] for option in result["options"]]
    assert first["origin"] == {"code": "AAA", "name": "Alpha Central"}
    assert first["departure"]["reported"].endswith("08:00:00+01:00")
    assert first["platform"] == {"scheduled": "4", "reported": "5"}
    assert first["crowd_forecast"] == "high"
    assert second["departure"]["reported"] is None
    assert second["platform"] == {"scheduled": None, "reported": None}
    assert second["crowd_forecast"] is None
    assert "07:48 scheduled, 08:00 reported" in result["text"]
    assert "08:10 scheduled" in result["text"]
    assert rig.rt.config is None and rig.rt.refusal is None
    assert (rig.rt.state, rig.store.writes, rig.rt.events_log) == before


async def test_quick_trip_rejects_ambiguous_names_without_querying_trips(tmp_path):
    rig = Rig(tmp_path, now="12:00")
    await rig.rt.start()
    rig.rt.known_stations = Stations(
        {"AAA": ("Alpha Central", "Alpha"), "BBB": ("Alpha West", "Alpha"), "RTD": ()}
    )

    result = await rig.rt.check_trip("Alpha", "RTD")

    assert isinstance(result, Refused)
    assert "AAA" in result.text and "BBB" in result.text
    assert rig.provider.calls == []


async def test_quick_trip_omits_unusable_changes_and_marks_unknown_arrival(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    first = make_leg("AAA", "UT", "07:55", "08:10", transfer_time=5)
    second = make_leg("UT", "RTD", "08:13", "08:58", number="2100")
    other_mode = replace(make_leg("AAA", "RTD", "08:00", "08:55"), alternative_transport=True)
    direct = make_leg("AAA", "RTD", "08:21", "09:00")
    direct = replace(
        direct, stops=(direct.board, replace(direct.alight, planned_arrival=None, arrival=None))
    )
    rig.provider.world = [
        make_connection([first, second]),
        make_connection([other_mode], status="ALTERNATIVE_TRANSPORT"),
        make_connection([direct]),
    ]

    result = await rig.rt.check_trip("AAA", "RTD")

    assert isinstance(result, dict)
    assert len(result["options"]) == 1
    assert result["options"][0]["legs"][0]["arrival"] == {"scheduled": None, "reported": None}
    assert "Arrives unavailable." in result["text"]


@pytest.mark.parametrize("flag", ["cancelled", "part_cancelled"])
async def test_quick_trip_keeps_trains_that_serve_both_requested_stops(tmp_path, flag):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    leg = make_leg("AAA", "RTD", "08:21", "09:00", stops=("UT",))
    leg = replace(
        leg,
        stops=(leg.board, replace(leg.stops[1], cancelled=True), leg.alight),
        **{flag: True},
    )
    rig.provider.world = [make_connection([leg])]

    result = await rig.rt.check_trip("AAA", "RTD")

    assert isinstance(result, dict)
    assert result["status"] == "ok"
    assert len(result["options"]) == 1


@pytest.mark.parametrize("forecasts", [("low", "high"), ("high", "low")])
async def test_conflicting_crowd_forecasts_are_unknown_in_quick_trip(tmp_path, forecasts):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    leg = make_leg("AAA", "RTD", "08:21", "09:00")
    rig.provider.world = [
        make_connection([replace(leg, crowd_forecast=value)]) for value in forecasts
    ]

    result = await rig.rt.check_trip("AAA", "RTD")

    assert isinstance(result, dict)
    (option,) = result["options"]
    assert option["legs"][0]["crowd_forecast"] is None
    assert "Crowd forecast" not in result["text"]


async def test_quick_trip_honors_any_profile_opt_out(tmp_path):
    rig = Rig(tmp_path, now="12:00")
    rig.write(
        text=PROFILE.replace(
            '    name: "office_b"', '    name: "office_b"\n    alerts: {quick_trip: false}'
        )
    )
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    calls = list(rig.provider.calls)

    result = await rig.rt.check_trip("UT", "RTD")

    assert isinstance(result, Refused)
    assert "disabled" in result.text
    assert rig.provider.calls == calls


async def test_quick_trip_throttles_overlap_and_respects_shared_ns_backoff(tmp_path, monkeypatch):
    rig = Rig(tmp_path, now="12:00")
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    asked, release = asyncio.Event(), asyncio.Event()
    ticks = 0.0
    monkeypatch.setattr("train_commute_companion.runtime.monotonic", lambda: ticks)

    async def hold(_):
        asked.set()
        await release.wait()

    rig.provider.hook = hold
    rig.provider.error = lambda _: RateLimited()
    first = asyncio.create_task(rig.rt.check_trip("AAA", "RTD"))
    try:
        await asyncio.wait_for(asked.wait(), 2)
        second = await rig.rt.check_trip("UT", "RTD")
        assert isinstance(second, Refused) and "30 seconds" in second.text
    finally:
        release.set()
        refusal = await first
    assert isinstance(refusal, Refused) and refusal.kind is RefusalKind.RETRY
    assert len(rig.provider.calls) == 1
    ticks += 30
    refusal = await rig.rt.check_trip("UT", "RTD")
    assert isinstance(refusal, Refused) and refusal.kind is RefusalKind.RETRY
    assert len(rig.provider.calls) == 1


async def test_quick_trip_distinguishes_stale_answer_from_empty_answer(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.rt.known_stations = CATALOGUE
    real = rig.provider.trips
    ticks = 0.0
    monkeypatch.setattr("train_commute_companion.runtime.monotonic", lambda: ticks)

    async def stale(query):
        got = await real(query)
        return replace(got, fetched_at=got.fetched_at - timedelta(minutes=5))

    monkeypatch.setattr(rig.provider, "trips", stale)
    result = await rig.rt.check_trip("UT", "RTD")
    assert isinstance(result, Refused) and "stale" in result.text.lower()
    monkeypatch.setattr(rig.provider, "trips", real)
    rig.provider.world = []
    ticks += 30
    result = await rig.rt.check_trip("UT", "RTD")
    assert isinstance(result, dict)
    assert result["status"] == "no_options" and not result["options"]

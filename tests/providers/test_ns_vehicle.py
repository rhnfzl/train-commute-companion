import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from train_commute_companion.calendar import TZ
from train_commute_companion.model import TrainId
from train_commute_companion.providers.base import AuthError, ProviderError, Query, RateLimited
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.providers.ns_vehicle import (
    ContextUnavailable,
    map_positions,
    map_train_context,
)
from train_commute_companion.serde import dumps, loads
from train_commute_companion.vehicle import FRESH_FOR, TrainContext, is_fresh, payload

DATA = Path(__file__).parents[1] / "data/ns"
NOW = datetime(2026, 11, 6, 21, 52, tzinfo=TZ)
TRAIN = TrainId(NOW.date(), "579", NOW)
CONTEXT = json.loads((DATA / "vehicle_context.json").read_text())
POSITIONS = json.loads((DATA / "vehicle_positions.json").read_text())


def test_public_context_keeps_capacity_equipment_and_unknown_measurement_evidence():
    context = map_train_context(CONTEXT, TRAIN, "UT", NOW)
    assert context.station == "UT" and context.platform == "12"
    assert context.carriage_count == 6 and context.length_meters == 161
    assert context.shortened is False and context.layout_direction == "right"
    assert context.units[0].capacity.second_class == 425
    assert context.units[0].capacity.bicycle_spaces == 6
    assert "TOILET" in context.units[0].equipment
    assert context.platform_features[0].description == "M"
    assert context.measurement_at is None and not context.operating_date_confirmed
    fields = payload(context, NOW)
    assert fields["fresh"] and not fields["capacity_is_availability"]
    assert not fields["equipment_operation_confirmed"] and not fields["boarding_confirmed"]
    assert "bakken" not in dumps(context) and "afbeelding" not in dumps(context)
    assert loads(TrainContext, dumps(context)) == context


def test_absence_and_explicit_zero_or_false_remain_distinct():
    raw = {"ritnummer": 579, "station": "UT"}
    absent = map_train_context(raw, TRAIN, "UT", NOW)
    explicit = map_train_context({**raw, "ingekort": False, "lengte": 0}, TRAIN, "UT", NOW)
    assert absent.shortened is None and absent.units is None and absent.platform_features is None
    assert explicit.shortened is False and explicit.carriage_count == 0
    raw["materieeldelen"] = [{"faciliteiten": [], "zitplaatsen": {"fietsplekken": 0}}]
    context = map_train_context(raw, TRAIN, "UT", NOW)
    assert context.units[0].equipment == ()
    assert context.units[0].capacity.bicycle_spaces == 0
    assert context.units[0].capacity.first_class is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("ritnummer", 1776),
        ("station", "ZL"),
        ("ingekort", "false"),
        ("lengte", -1),
        ("lengte", True),
        ("lengte", 2.5),
        ("lengteInMeters", float("nan")),
        ("materieeldelen", {}),
        ("perronVoorzieningen", [{}]),
        ("rijrichting", []),
    ],
)
def test_invalid_or_mismatched_context_fails_closed(field, value):
    with pytest.raises(ContextUnavailable):
        map_train_context({**CONTEXT, field: value}, TRAIN, "UT", NOW)


@pytest.mark.parametrize("value", [-1, True, 1.5, "6", 10**20])
def test_invalid_capacity_is_not_seat_availability(value):
    raw = deepcopy(CONTEXT)
    raw["materieeldelen"][0]["zitplaatsen"]["fietsplekken"] = value
    with pytest.raises(ContextUnavailable):
        map_train_context(raw, TRAIN, "UT", NOW)


def test_positions_remain_unbound_candidates_and_public_payload_is_serializable():
    positions = map_positions(POSITIONS, NOW)
    assert positions.positions[0].train_number == "579"
    assert positions.positions[0].source == "OBIS"
    assert positions.positions[0].measurement_at is None
    assert not positions.positions[0].operating_date_confirmed
    json.dumps(payload(positions, NOW))
    assert not is_fresh(positions, NOW - timedelta(seconds=1))
    assert not is_fresh(positions, NOW + FRESH_FOR)
    assert not is_fresh(positions, NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "field,value",
    [
        ("lat", float("nan")),
        ("lat", True),
        ("lng", 181),
        ("lng", "5.1"),
        ("richting", 361),
        ("snelheid", -1),
        ("horizontaleNauwkeurigheid", float("inf")),
    ],
)
def test_invalid_position_fields_fail_closed(field, value):
    raw = deepcopy(POSITIONS)
    raw["payload"]["treinen"][0][field] = value
    with pytest.raises(ContextUnavailable):
        map_positions(raw, NOW)


def test_duplicate_service_positions_are_ambiguous():
    raw = deepcopy(POSITIONS)
    raw["payload"]["treinen"].append(raw["payload"]["treinen"][0])
    with pytest.raises(ContextUnavailable, match="ambiguous"):
        map_positions(raw, NOW)


async def test_each_context_method_is_one_request_to_fixed_host_and_station_scope():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json=POSITIONS if request.url.path.endswith("/vehicle") else CONTEXT
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NsProvider("test", client, lambda: NOW)
        await provider.train_context(TRAIN, "UT")
        await provider.vehicle_positions()
        await provider.vehicle_positions(52.08889, 5.110278)
    assert len(requests) == 3
    assert requests[0].url.path == "/virtual-train-api/v1/trein/579/UT"
    assert requests[0].url.params["dateTime"] == NOW.isoformat(timespec="seconds")
    assert not requests[1].url.params
    assert requests[2].url.params["lat"] == "52.08889"
    assert all(request.url.host == "gateway.apiportal.ns.nl" for request in requests)


@pytest.mark.parametrize(
    "date_offset,station", [(1, "UT"), (-1, "UT"), (0, "@place:x"), (0, "UT/evil")]
)
async def test_foreign_dates_and_unsafe_station_paths_never_make_a_request(date_offset, station):
    def handler(request):
        pytest.fail("invalid train context request reached HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NsProvider("test", client, lambda: NOW)
        train = replace(TRAIN, operating_date=NOW.date() + timedelta(days=date_offset))
        with pytest.raises(ContextUnavailable):
            await provider.train_context(train, station)


@pytest.mark.parametrize(
    "train",
    [
        replace(TRAIN, train_number="579/other"),
        replace(TRAIN, planned_departure=NOW + timedelta(days=1)),
        replace(TRAIN, planned_departure=NOW.replace(tzinfo=None)),
    ],
)
async def test_inconsistent_or_invalid_service_identity_never_makes_a_request(train):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("HTTP"))
    ) as client:
        with pytest.raises(ContextUnavailable):
            await NsProvider("test", client, lambda: NOW).train_context(train, "UT")


async def test_context_crossing_local_midnight_is_not_attached_to_the_old_service_date():
    times = iter((NOW, NOW + timedelta(days=1)))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=CONTEXT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ContextUnavailable, match="current operating date"):
            await NsProvider("test", client, lambda: next(times)).train_context(TRAIN, "UT")
    assert len(requests) == 1


@pytest.mark.parametrize("lat,lon", [(None, 5.1), (True, 5.1), (91, 5.1), (52, float("inf"))])
async def test_invalid_area_never_makes_a_request(lat, lon):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("HTTP"))
    ) as client:
        with pytest.raises(ContextUnavailable):
            await NsProvider("test", client, lambda: NOW).vehicle_positions(lat, lon)


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
async def test_optional_product_failure_does_not_reject_main_key_or_prevent_station_query(status):
    def handler(request):
        if request.url.path.startswith("/virtual-train-api"):
            return httpx.Response(status, text="private key and provider exception details")
        return httpx.Response(200, json=json.loads((DATA / "on_time_direct.json").read_text()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NsProvider("test", client, lambda: NOW)
        with pytest.raises(ContextUnavailable) as error:
            await provider.train_context(TRAIN, "UT")
        assert "private" not in str(error.value)
        assert not isinstance(error.value, AuthError)
        assert (await provider.trips(Query("AAA", "BBB", NOW))).connections


async def test_optional_rate_limit_retains_shared_retry_after():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "42"}))
    ) as client:
        with pytest.raises(RateLimited) as error:
            await NsProvider("test", client, lambda: NOW).vehicle_positions()
    assert error.value.retry_after == 42


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://untrusted.invalid/private"}),
        httpx.Response(200, content=b"invalid private JSON"),
        httpx.Response(200, content=b" " * 2_000_001),
    ],
)
async def test_redirect_invalid_json_and_oversized_context_are_safe(response):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ContextUnavailable) as error:
            await NsProvider("test", client, lambda: NOW).vehicle_positions()
    assert len(requests) == 1 and "private" not in str(error.value)


async def test_optional_timeout_remains_a_safe_provider_error():
    def handler(request):
        raise httpx.ReadTimeout("private-token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await NsProvider("test", client, lambda: NOW).vehicle_positions()
    assert "private-token" not in str(error.value)

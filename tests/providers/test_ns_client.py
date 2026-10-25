import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from builders import AMS

from train_commute_companion.providers.base import AuthError, ProviderError, Query, RateLimited
from train_commute_companion.providers.ns import NsProvider

NOW = datetime(2026, 10, 27, 7, 0, tzinfo=AMS)
Q = Query("AAA", "BBB", datetime(2026, 10, 27, 8, 0, tzinfo=AMS), via="UT")
DATA = Path(__file__).parent.parent / "data" / "ns"


def provider(handler) -> NsProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NsProvider("secret-key", client, lambda: NOW)


def status(code: int, headers: dict[str, str] | None = None, body: bytes = b"{}"):
    return lambda req: httpx.Response(code, headers=headers, content=body)


async def test_trips_sends_key_header_and_maps():
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, content=(DATA / "on_time_direct.json").read_bytes())

    a = await provider(h).trips(Q)
    req = seen[0]
    assert req.headers["Ocp-Apim-Subscription-Key"] == "secret-key"
    assert req.url.path == "/reisinformatie-api/api/v3/trips"
    p = req.url.params
    assert (p["fromStation"], p["toStation"], p["viaStation"]) == ("AAA", "BBB", "UT")
    assert p["dateTime"].startswith("2026-10-27T08:00:00")
    assert a.fetched_at == NOW and a.connections


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_error_without_key_in_message(code):
    with pytest.raises(AuthError) as e:
        await provider(status(code)).trips(Q)
    assert "secret-key" not in str(e.value)


async def test_429_carries_retry_after():
    with pytest.raises(RateLimited) as e:
        await provider(status(429, {"Retry-After": "120"})).trips(Q)
    assert e.value.retry_after == 120


async def test_429_without_header_has_no_retry_after():
    with pytest.raises(RateLimited) as e:
        await provider(status(429)).trips(Q)
    assert e.value.retry_after is None


async def test_5xx_is_provider_error():
    with pytest.raises(ProviderError):
        await provider(status(503)).trips(Q)


async def test_timeout_is_provider_error_without_key():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ProviderError) as e:
        await provider(h).trips(Q)
    assert "secret-key" not in str(e.value)


async def test_invalid_json_is_provider_error():
    with pytest.raises(ProviderError):
        await provider(status(200, body=b"<html>")).trips(Q)


async def test_planned_works_maps_and_queries_inactive():
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, content=(DATA / "disruptions.json").read_bytes())

    pw = await provider(h).planned_works()
    assert seen[0].url.params["isActive"] == "false"
    assert pw.coverage in ("complete", "unknown")
    assert json.loads((DATA / "disruptions.json").read_text()) is not None


@pytest.mark.parametrize("handler", [status(500), status(429), status(200, body=b"nope")])
async def test_planned_works_failure_returns_unknown_coverage(handler):
    pw = await provider(handler).planned_works()
    assert pw.coverage == "unknown" and pw.items == ()


async def test_planned_works_auth_error_still_raises():
    with pytest.raises(AuthError):
        await provider(status(401)).planned_works()


async def test_stations_maps():
    s = await provider(
        lambda req: httpx.Response(200, content=(DATA / "stations.json").read_bytes())
    ).stations()
    assert s

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from lifecycle_fixtures import at

from train_commute_companion.providers.base import DoorToDoorNotAllowed, ProviderError, Query
from train_commute_companion.providers.ns import NsProvider

DATA = Path(__file__).parents[1] / "data/ns/on_time_direct.json"
Q = Query("AAA", "BBB", at("08:00"))


@pytest.mark.parametrize("field", ["origin", "destination", "via"])
@pytest.mark.parametrize("value", ["@place:physical-stop", "8400002"])
async def test_non_station_identity_never_reaches_ns(field, value):
    def handler(request):
        pytest.fail("non-station identity reached the station planner")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NsProvider("test", client, lambda: Q.at)
        with pytest.raises(ProviderError, match="non-station locations"):
            await provider.trips(replace(Q, **{field: value}))


async def test_door_to_door_refusal_does_not_disable_station_queries():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "errors": [
                        {
                            "type": "API_KEY_NOT_ALLOWED_TO_PLAN_DOOR_TO_DOOR",
                            "message": "untrusted provider detail",
                        }
                    ]
                },
            )
        return httpx.Response(200, json=json.loads(DATA.read_text()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NsProvider("test", client, lambda: Q.at)
        with pytest.raises(DoorToDoorNotAllowed) as exc:
            await provider.trips(Q)
        assert "untrusted provider detail" not in str(exc.value)
        assert (await provider.trips(Q)).connections
    assert len(requests) == 2
    assert requests[1].url.params["fromStation"] == "AAA"
    assert requests[1].url.params["toStation"] == "BBB"


@pytest.mark.parametrize(
    "body",
    [
        {"errors": [{"type": "SOME_OTHER_ERROR"}]},
        {"errors": [{"message": "API_KEY_NOT_ALLOWED_TO_PLAN_DOOR_TO_DOOR"}]},
        {"errors": "API_KEY_NOT_ALLOWED_TO_PLAN_DOOR_TO_DOOR"},
        {"errors": [None]},
        [],
    ],
)
async def test_other_bad_requests_do_not_become_capability_refusals(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json=body))
    ) as client:
        provider = NsProvider("test", client, lambda: Q.at)
        with pytest.raises(ProviderError) as exc:
            await provider.trips(Q)
    assert type(exc.value) is ProviderError

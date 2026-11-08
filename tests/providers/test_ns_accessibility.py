import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from lifecycle_fixtures import at

from train_commute_companion.model import Stop
from train_commute_companion.providers.base import Query
from train_commute_companion.providers.ns import NsProvider, map_trips
from train_commute_companion.serde import dumps, loads

DATA = Path(__file__).parents[1] / "data/ns/on_time_direct.json"
Q = Query("AAA", "BBB", at("08:00"))


@pytest.mark.parametrize("enabled", [False, True])
async def test_accessible_trip_filter_is_explicit(enabled):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=json.loads(DATA.read_text()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NsProvider("test", client, lambda: Q.at)
        await provider.trips(replace(Q, require_accessible=enabled))
    assert requests[0].url.params.get("searchForAccessibleTrip") == ("true" if enabled else None)


def test_endpoint_assistance_notes_and_exit_side_survive_listed_stops():
    payload = json.loads(DATA.read_text())
    leg = payload["trips"][0]["legs"][0]
    leg["origin"]["notes"] = [
        {"key": "SS", "noteType": "ATTRIBUTE", "value": "Book boarding assistance"}
    ]
    leg["destination"]["notes"] = [
        {"key": "SS", "noteType": "ATTRIBUTE", "value": "Book alighting assistance"}
    ]
    leg["destination"]["exitSide"] = "RIGHT"
    result = map_trips(payload, Q, Q.at).connections[0].legs[0]
    assert result.board.accessibility_notes == ("Book boarding assistance",)
    assert result.stops[-1].accessibility_notes == ("Book alighting assistance",)
    assert result.stops[-1].exit_side == "right"
    assert loads(Stop, dumps(result.stops[-1])) == result.stops[-1]


def test_unknown_accessibility_remains_unknown():
    payload = json.loads(DATA.read_text())
    leg = payload["trips"][0]["legs"][0]
    leg["destination"]["exitSide"] = "MAYBE"
    leg["origin"]["notes"] = [{"key": "OTHER", "noteType": "ATTRIBUTE", "value": "accessible"}]
    result = map_trips(payload, Q, Q.at).connections[0].legs[0]
    assert result.board.accessibility_notes == ()
    assert result.stops[-1].exit_side is None


@pytest.mark.parametrize(
    "endpoint,listed,expected",
    [("RIGHT", "LEFT", None), ("LEFT", "LEFT", "left"), ("MAYBE", "RIGHT", "right")],
)
def test_conflicting_endpoint_and_listed_exit_sides_remain_unknown(endpoint, listed, expected):
    payload = json.loads(DATA.read_text())
    leg = payload["trips"][0]["legs"][0]
    leg["destination"]["exitSide"] = endpoint
    leg["stops"][-1]["exitSide"] = listed
    result = map_trips(payload, Q, Q.at).connections[0].legs[0]
    assert result.alight.exit_side == expected

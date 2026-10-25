import json
from datetime import datetime
from pathlib import Path

import pytest
from builders import AMS

from train_commute_companion.model import Provenance
from train_commute_companion.providers.base import MalformedAnswer, Query
from train_commute_companion.providers.ns import map_disruptions, map_stations, map_trips

NOW = datetime(2026, 10, 27, 7, 0, tzinfo=AMS)
Q = Query("AAA", "BBB", datetime(2026, 10, 27, 8, 0, tzinfo=AMS))


def fixture(name: str) -> object:
    return json.loads((Path(__file__).parent.parent / "data" / "ns" / name).read_text())


def test_actual_equal_to_planned_is_reported():
    a = map_trips(fixture("on_time_direct.json"), Q, NOW)
    dep = a.connections[0].legs[0].board.departure
    assert dep.provenance is Provenance.REPORTED and dep.value == dep.planned
    assert a.connections[0].legs[0].train.train_number == "1234"
    assert a.connections[0].origin == "AAA" and a.connections[0].is_train_only


def test_planned_only_is_planned_not_reported():
    dep = map_trips(fixture("planned_only.json"), Q, NOW).connections[0].legs[0].board.departure
    assert dep.provenance is Provenance.PLANNED and dep.value is None


def test_transfer_fields():
    c = map_trips(fixture("one_transfer.json"), Q, NOW).connections[0]
    leg0, leg1 = c.legs
    assert (
        leg0.transfer_time_to_next == 2 and leg1.transfer_message_min == 3 and leg0.cross_platform
    )
    assert c.transfer_stations == ("UT",)


def test_missing_trips_key_is_malformed():
    with pytest.raises(MalformedAnswer):
        map_trips({"foo": 1}, Q, NOW)


@pytest.mark.parametrize(
    "bad", [{"trips": "x"}, {"trips": [{"legs": []}]}, {"trips": [{"legs": [{"x": 1}]}]}, []]
)
def test_other_malformed_shapes(bad):
    with pytest.raises(MalformedAnswer):
        map_trips(bad, Q, NOW)


def test_empty_trips_is_healthy_and_empty():
    assert map_trips({"trips": []}, Q, NOW).connections == ()


def test_cancelled_trip_without_durations_maps():
    c = map_trips(fixture("cancelled_no_durations.json"), Q, NOW).connections[0]
    assert c.status == "CANCELLED" and c.legs[0].cancelled and c.legs[0].alight.cancelled


def test_bus_leg_makes_connection_not_train_only():
    c = map_trips(fixture("bus_leg.json"), Q, NOW).connections[0]
    assert not c.is_train_only and c.legs[1].alternative_transport


def test_leg_without_stops_uses_origin_and_destination():
    raw = json.loads(json.dumps(fixture("on_time_direct.json")))
    del raw["trips"][0]["legs"][0]["stops"]
    leg = map_trips(raw, Q, NOW).connections[0].legs[0]
    assert leg.board.code == "AAA" and leg.alight.arrival.provenance is Provenance.REPORTED


def test_disruptions_maintenance_items_and_coverage():
    w = map_disruptions(fixture("disruptions.json"))
    assert w.coverage == "complete" and len(w.items) == 1 and w.items[0].stations == {"AAA", "BBB"}


def test_disruptions_malformed_is_unknown_coverage():
    assert map_disruptions({"x": 1}).coverage == "unknown"


def test_disruption_item_without_dates_is_unknown_coverage():
    assert map_disruptions([{"type": "MAINTENANCE", "title": "x"}]).coverage == "unknown"


def test_stations():
    assert "UT" in map_stations(fixture("stations.json"))
    with pytest.raises(MalformedAnswer):
        map_stations({"payload": []})

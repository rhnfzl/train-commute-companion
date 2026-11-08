import json
from pathlib import Path

from lifecycle_fixtures import at

from train_commute_companion.providers.base import Query
from train_commute_companion.providers.ns import map_trips

DATA = Path(__file__).parents[1] / "data/ns/on_time_direct.json"
QUERY = Query("AAA", "BBB", at("08:00"))


def payload():
    result = json.loads(DATA.read_text())
    leg = result["trips"][0]["legs"][0]
    leg["product"].update(type="BUS", operatorName="NS")
    leg["origin"].update(lat=52.0175, lng=4.7044)
    leg["destination"].update(lat=52.0184, lng=4.7060)
    return result, leg


def test_endpoint_physical_positions_survive_stop_list_without_coordinates():
    data, _ = payload()
    leg = map_trips(data, QUERY, QUERY.at).connections[0].legs[0]
    assert leg.transport_mode == "BUS" and leg.operator == "NS"
    assert leg.board.physical_id and leg.alight.physical_id
    assert leg.board.physical_id != leg.alight.physical_id
    assert (leg.board.latitude, leg.board.longitude) == (52.0175, 4.7044)
    assert (leg.alight.latitude, leg.alight.longitude) == (52.0184, 4.7060)


def test_conflicting_stop_and_endpoint_coordinates_are_not_a_confirmed_place():
    data, raw = payload()
    raw["stops"][0].update(lat=52.0184, lng=4.7060)
    board = map_trips(data, QUERY, QUERY.at).connections[0].legs[0].board
    assert board.physical_id is None and board.latitude is None and board.longitude is None


def test_adopted_endpoint_identity_keeps_its_exact_boarding_name():
    data, raw = payload()
    raw["origin"]["name"] = "Exact replacement bus boarding street"
    board = map_trips(data, QUERY, QUERY.at).connections[0].legs[0].board
    assert board.name == "Exact replacement bus boarding street"
    assert board.physical_id is not None


def test_invalid_coordinates_and_unknown_mode_remain_unknown():
    data, raw = payload()
    raw["origin"].update(lat=float("nan"))
    raw["product"]["type"] = "UNKNOWN"
    leg = map_trips(data, QUERY, QUERY.at).connections[0].legs[0]
    assert leg.transport_mode is None
    assert leg.board.physical_id is None and leg.board.latitude is None


def test_saved_ns_works_plan_keeps_same_station_rail_and_bus_places_distinct():
    data = json.loads(DATA.with_name("multimodal_physical.json").read_text())
    legs = map_trips(data, QUERY, QUERY.at).connections[0].legs
    assert [leg.transport_mode for leg in legs] == [
        "TRAIN",
        "WALK",
        "BUS",
        "WALK",
        "METRO",
        "METRO",
    ]
    train, walk, bus = legs[:3]
    assert train.alight.code == walk.board.code == walk.alight.code == bus.board.code == "GD"
    assert train.alight.physical_id == walk.board.physical_id
    assert walk.alight.physical_id == bus.board.physical_id
    assert walk.board.physical_id != walk.alight.physical_id
    assert bus.operator == "NS" and bus.service_reference

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from builders import AMS

from train_commute_companion.model import Provenance, TrainId
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
    assert w.coverage == "complete" and len(w.items) == 1 and w.items[0].stations >= {"AAA", "BBB"}


def test_disruptions_malformed_is_unknown_coverage():
    assert map_disruptions({"x": 1}).coverage == "unknown"


def test_disruption_item_without_dates_is_unknown_coverage():
    assert map_disruptions([{"type": "MAINTENANCE", "title": "x"}]).coverage == "unknown"


def test_stations():
    assert "UT" in map_stations(fixture("stations.json"))
    with pytest.raises(MalformedAnswer):
        map_stations({"payload": []})


@pytest.mark.parametrize(
    "sections",
    [
        [{"consequence": "x"}],
        [{"section": {"stations": [{}]}}],
        [{"section": {"stations": ["x"]}}],
        [{"section": "x"}],
        [{"section": {"stations": "x"}}],
        [{"consequence": {"section": {"stations": 5}}}],
        ["x"],
        "x",
    ],
)
def test_disruption_nested_malformed_is_unknown_coverage(sections):
    d = {
        "type": "MAINTENANCE",
        "start": "2026-01-01T00:00:00+0100",
        "end": "2026-01-02T00:00:00+0100",
        "publicationSections": sections,
    }
    assert map_disruptions([d]).coverage == "unknown"


def test_maintenance_station_matched_by_uic_code():
    d = {
        "type": "MAINTENANCE",
        "start": "2026-01-01T00:00:00+0100",
        "end": "2026-01-02T00:00:00+0100",
        "publicationSections": [{"section": {"stations": [{"uicCode": "1000002"}]}}],
    }
    w = map_disruptions([d])
    assert w.coverage == "complete" and "1000002" in w.items[0].stations


def _direct() -> dict:
    return json.loads(json.dumps(fixture("on_time_direct.json")))


def _leg0(raw: dict) -> dict:
    return raw["trips"][0]["legs"][0]


@pytest.mark.parametrize(
    "stops",
    [
        [{"uicCode": "1000001", "cancelled": True}, None],
        [{"uicCode": "1000001"}, "x"],
        "x",
        {"uicCode": "1000001"},
        5,
        [{"uicCode": "1000001", "plannedDepartureDateTime": "2026-10-27T08:12:00+0100"}],
    ],
)
def test_malformed_stop_list_is_malformed_not_endpoint_fallback(stops):
    raw = _direct()
    _leg0(raw)["stops"] = stops
    with pytest.raises(MalformedAnswer):
        map_trips(raw, Q, NOW)


def test_empty_stop_list_falls_back_to_endpoints():
    raw = _direct()
    _leg0(raw)["stops"] = []
    assert map_trips(raw, Q, NOW).connections[0].legs[0].board.code == "AAA"


def test_endpoint_fallback_keeps_cancelled_flag():
    raw = _direct()
    del _leg0(raw)["stops"]
    _leg0(raw)["destination"]["cancelled"] = True
    assert map_trips(raw, Q, NOW).connections[0].legs[0].alight.cancelled


def test_stop_without_any_identifier_is_malformed():
    raw = _direct()
    del _leg0(raw)["stops"][1]["uicCode"]
    with pytest.raises(MalformedAnswer):
        map_trips(raw, Q, NOW)


@pytest.mark.parametrize("number", [None, "", "  ", {}, [], True, 1.5])
def test_train_without_usable_number_is_malformed(number):
    raw = _direct()
    _leg0(raw)["product"]["number"] = number
    with pytest.raises(MalformedAnswer):
        map_trips(raw, Q, NOW)


def test_train_missing_number_is_malformed():
    raw = _direct()
    del _leg0(raw)["product"]["number"]
    with pytest.raises(MalformedAnswer):
        map_trips(raw, Q, NOW)


def test_integer_train_number_maps_to_same_identity_as_string():
    raw = _direct()
    _leg0(raw)["product"]["number"] = 1234
    a = map_trips(raw, Q, NOW).connections[0].legs[0].train
    b = map_trips(fixture("on_time_direct.json"), Q, NOW).connections[0].legs[0].train
    assert isinstance(a, TrainId) and a == b


def test_bus_leg_without_number_still_maps():
    c = map_trips(fixture("bus_leg.json"), Q, NOW).connections[0]
    assert c.legs[1].alternative_transport


def test_actual_only_times_are_reported_not_dropped():
    raw = _direct()
    stops = _leg0(raw)["stops"]
    del stops[1]["plannedArrivalDateTime"]
    leg = map_trips(raw, Q, NOW).connections[0].legs[0]
    arr = leg.alight.arrival
    assert arr is not None and arr.provenance is Provenance.REPORTED
    assert arr.planned is None and arr.value is not None


def test_actual_only_endpoint_time_is_reported():
    raw = _direct()
    del _leg0(raw)["stops"]
    del _leg0(raw)["destination"]["plannedDateTime"]
    arr = map_trips(raw, Q, NOW).connections[0].legs[0].alight.arrival
    assert arr is not None and arr.provenance is Provenance.REPORTED and arr.planned is None


@pytest.mark.parametrize("field", ["messages", "transferMessages"])
@pytest.mark.parametrize("value", [5, True, "text", {"text": "x"}, ["text"], [5]])
def test_malformed_message_collections_are_malformed(field, value):
    raw = _direct()
    _leg0(raw)[field] = value
    with pytest.raises(MalformedAnswer):
        map_trips(raw, Q, NOW)


def test_null_message_collections_are_tolerated():
    raw = _direct()
    _leg0(raw)["messages"] = None
    _leg0(raw)["transferMessages"] = None
    assert map_trips(raw, Q, NOW).connections[0].legs[0].messages == ()


GOOD_SECTION = {"section": {"stations": [{"uicCode": "1000002", "stationCode": "UT"}]}}


def _maint(**over: object) -> dict:
    d = {
        "type": "MAINTENANCE",
        "start": "2026-01-01T00:00:00+0100",
        "end": "2026-01-02T00:00:00+0100",
        "publicationSections": [GOOD_SECTION],
    }
    d.update(over)
    return {k: v for k, v in d.items() if v is not None}


def test_well_formed_maintenance_is_complete():
    w = map_disruptions([_maint()])
    assert w.coverage == "complete" and w.items[0].stations == {"1000002", "UT"}


@pytest.mark.parametrize(
    "item",
    [
        {"title": "no type"},
        {"type": None},
        {"type": 5},
        {"type": ""},
        _maint(publicationSections=None),
        _maint(publicationSections=[]),
        _maint(publicationSections=[{}]),
        _maint(publicationSections=[{"consequence": {"description": "x"}}]),
        _maint(publicationSections=[{"section": {}}]),
        _maint(publicationSections=[{"section": {"stations": []}}]),
        _maint(publicationSections=[{"section": {"stations": [{"stationCode": "UT"}]}}]),
        _maint(end="2025-12-31T00:00:00+0100"),
        _maint(end="2026-01-01T00:00:00+0100"),
    ],
)
def test_unreadable_maintenance_item_is_unknown_coverage(item):
    assert map_disruptions([item]).coverage == "unknown"


def test_other_typed_items_are_skipped_and_complete():
    assert map_disruptions([{"type": "CALAMITY"}]) == map_disruptions([])


def test_maintenance_by_uic_finds_intermediate_trip_stop_without_station_code():
    raw = _direct()
    stops = _leg0(raw)["stops"]
    stops.insert(1, {"uicCode": "1000002", "name": "Utrecht Centraal"})
    stop = map_trips(raw, Q, NOW).connections[0].legs[0].stops[1]
    work = map_disruptions(
        [_maint(publicationSections=[{"section": {"stations": [{"uicCode": "1000002"}]}}])]
    ).items[0]
    assert work.lists(stop)


def test_maintenance_with_both_ids_finds_trip_endpoint_by_station_code():
    leg = map_trips(fixture("on_time_direct.json"), Q, NOW).connections[0].legs[0]
    w = map_disruptions(fixture("disruptions.json"))
    assert w.coverage == "complete" and w.items[0].lists(leg.board) and w.items[0].lists(leg.alight)
    assert leg.board.code == "AAA" and leg.board.uic == "1000001"


def test_maintenance_elsewhere_does_not_list_trip_stops():
    leg = map_trips(fixture("on_time_direct.json"), Q, NOW).connections[0].legs[0]
    work = map_disruptions([_maint()]).items[0]
    assert not any(work.lists(s) for s in leg.stops)


UIC_ONLY_WORK = _maint(publicationSections=[{"section": {"stations": [{"uicCode": "1000001"}]}}])


def _code_only(p: dict) -> None:
    p.pop("uicCode", None)
    p["stationCode"] = "AAA"


def test_code_only_stop_resolves_uic_from_trip_endpoint_alias():
    raw = _direct()
    _code_only(_leg0(raw)["stops"][0])
    leg = map_trips(raw, Q, NOW).connections[0].legs[0]
    assert leg.stops[0].uic == "1000001"
    assert map_disruptions([UIC_ONLY_WORK]).items[0].lists(leg.stops[0])


def test_code_only_endpoint_resolves_uic_from_trip_stop_alias():
    raw = _direct()
    leg_raw = _leg0(raw)
    _code_only(leg_raw["origin"])
    del leg_raw["stops"]
    raw["trips"][0]["legs"].append(json.loads(json.dumps(_leg0(_direct()))))
    raw["trips"][0]["legs"][1]["stops"][0]["stationCode"] = "AAA"
    leg = map_trips(raw, Q, NOW).connections[0].legs[0]
    assert leg.board.uic == "1000001"
    assert map_disruptions([UIC_ONLY_WORK]).items[0].lists(leg.board)


@pytest.mark.parametrize("where", ["stop", "endpoint"])
def test_unresolved_stop_identity_is_never_clean(where):
    raw = _direct()
    leg_raw = _leg0(raw)
    for p in (leg_raw["origin"], *leg_raw["stops"]):
        _code_only(p)
    if where == "endpoint":
        del leg_raw["stops"]
    stop = map_trips(raw, Q, NOW).connections[0].legs[0].stops[0]
    assert stop.uic is None
    elsewhere = _maint()  # names 1000002/UT only, yet cannot rule out a uic-less stop
    assert map_disruptions([elsewhere]).items[0].lists(stop)


def test_maintenance_end_before_start_across_dst_fold_is_unknown():
    # 02:10+01:00 is 01:10 UTC, 02:50+02:00 is 00:50 UTC: reversed although the wall clock rises
    item = _maint(start="2026-10-25T02:10:00+0100", end="2026-10-25T02:50:00+0200")
    assert map_disruptions([item]).coverage == "unknown"


def test_maintenance_crossing_the_repeated_hour_is_complete():
    # 02:50+02:00 is 00:50 UTC, 02:10+01:00 is 01:10 UTC: valid although the wall clock falls
    item = _maint(start="2026-10-25T02:50:00+0200", end="2026-10-25T02:10:00+0100")
    w = map_disruptions([item])
    assert w.coverage == "complete"
    assert w.items[0].end - w.items[0].start == timedelta(minutes=20)

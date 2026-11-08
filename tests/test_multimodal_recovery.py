from dataclasses import replace

import pytest
from builders import make_connection, make_leg
from lifecycle_fixtures import at

from train_commute_companion.model import Provenance
from train_commute_companion.recovery import (
    RecoveryError,
    from_connection,
    render,
    validate_catchability,
)


def step(mode, origin, destination, departure, arrival, *, provenance=Provenance.REPORTED):
    leg = make_leg(origin[0], destination[0], departure, arrival, provenance=provenance)
    stops = tuple(
        replace(stop, name=data[1], physical_id=data[2], latitude=data[3], longitude=data[4])
        for stop, data in zip(leg.stops, (origin, destination), strict=True)
    )
    return replace(
        leg,
        stops=stops,
        transport_mode=mode,
        operator=None if mode == "WALK" else "NS",
        alternative_transport=mode != "TRAIN",
    )


RAIL = ("GD", "Gouda", "rail", 52.0175018310547, 4.70444440841675)
BUS = ("GD", "Gouda replacement bus stop", "bus", 52.018418, 4.70597)
END = ("RTD", "Rotterdam Centraal", "end", 51.924651, 4.469868)


def sample(*, provenance=Provenance.REPORTED):
    return make_connection(
        [
            step("WALK", RAIL, BUS, "08:00", "08:05", provenance=provenance),
            step("BUS", BUS, END, "08:10", "09:00", provenance=provenance),
        ]
    )


def test_walk_keeps_distinct_places_with_the_same_station_code():
    itinerary = from_connection(sample())
    assert [s.mode for s in itinerary.steps] == ["WALK", "BUS"]
    walk = itinerary.steps[0]
    assert walk.origin.station_code == walk.destination.station_code == "GD"
    assert walk.origin.physical_id != walk.destination.physical_id
    assert walk.destination.name == BUS[1]
    assert walk.destination.latitude == BUS[3]
    assert itinerary.steps[1].operator == "NS"
    assert itinerary.vehicle_changes == 0
    assert not hasattr(itinerary.steps[1], "train")
    text = render(itinerary)
    assert "Walk" in text and BUS[1] in text and "Bus" in text and "NS" in text
    assert "boarded" not in text and "arrived" not in text


def test_vehicle_changes_ignore_walk_steps():
    first = step("TRAIN", END, RAIL, "07:00", "08:00")
    itinerary = from_connection(make_connection([first, *sample().legs]))
    assert itinerary.vehicle_changes == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: replace(c, legs=()),
        lambda c: replace(c, legs=(replace(c.legs[0], transport_mode=None), *c.legs[1:])),
        lambda c: replace(c, legs=(replace(c.legs[0], cancelled=True), *c.legs[1:])),
        lambda c: replace(c, legs=(replace(c.legs[0], reachable=False), *c.legs[1:])),
        lambda c: replace(c, legs=(replace(c.legs[0], stops=()), *c.legs[1:])),
        lambda c: replace(
            c,
            legs=(
                replace(
                    c.legs[0], stops=(replace(c.legs[0].board, physical_id=None), c.legs[0].alight)
                ),
                *c.legs[1:],
            ),
        ),
        lambda c: replace(
            c,
            legs=(
                replace(
                    c.legs[0],
                    stops=(replace(c.legs[0].board, latitude=float("nan")), c.legs[0].alight),
                ),
                *c.legs[1:],
            ),
        ),
        lambda c: replace(
            c,
            legs=(
                replace(
                    c.legs[0],
                    stops=(replace(c.legs[0].board, longitude=float("inf")), c.legs[0].alight),
                ),
                *c.legs[1:],
            ),
        ),
        lambda c: replace(
            c,
            legs=(
                replace(
                    c.legs[0],
                    stops=(
                        replace(c.legs[0].board, planned_departure=None, departure=None),
                        c.legs[0].alight,
                    ),
                ),
                *c.legs[1:],
            ),
        ),
    ],
)
def test_incomplete_or_unsafe_itineraries_are_refused(mutate):
    with pytest.raises(RecoveryError):
        from_connection(mutate(sample()))


def test_station_code_does_not_bridge_a_missing_walk():
    bus = step("BUS", BUS, END, "08:10", "09:00")
    train = step("TRAIN", END, RAIL, "07:00", "08:00")
    with pytest.raises(RecoveryError, match="physical"):
        from_connection(make_connection([train, bus]))


def test_reversed_leg_and_impossible_transfer_are_refused():
    with pytest.raises(RecoveryError, match="time"):
        from_connection(make_connection([step("BUS", BUS, END, "09:10", "09:00")]))
    walk, bus = sample().legs
    bus = step("BUS", BUS, END, "08:04", "09:00")
    with pytest.raises(RecoveryError, match="time"):
        from_connection(make_connection([walk, bus]))


def test_planned_only_data_is_unconfirmed_and_not_progress_evidence():
    itinerary = from_connection(sample(provenance=Provenance.PLANNED))
    assert "unconfirmed" in render(itinerary)
    result = validate_catchability(
        itinerary, itinerary.steps[0].origin, at("07:59"), at("07:59"), checked_at=at("07:59")
    )
    assert result.state == "unconfirmed"
    later = validate_catchability(
        itinerary, itinerary.steps[0].origin, at("09:01"), at("09:01"), checked_at=at("09:01")
    )
    assert later.state == "not_catchable"
    assert "arrived" not in render(itinerary)


def test_catchability_requires_current_confirmed_physical_origin():
    itinerary = from_connection(sample())
    origin = itinerary.steps[0].origin
    assert (
        validate_catchability(
            itinerary, origin, at("07:59"), at("07:59"), checked_at=at("07:59")
        ).state
        == "feasible"
    )
    other = replace(origin, physical_id="bus", latitude=BUS[3], longitude=BUS[4])
    assert (
        validate_catchability(
            itinerary, other, at("07:59"), at("07:59"), checked_at=at("07:59")
        ).state
        == "unknown"
    )
    assert (
        validate_catchability(itinerary, None, None, at("07:59"), checked_at=at("07:59")).state
        == "unknown"
    )
    assert (
        validate_catchability(
            itinerary, origin, at("07:40"), at("07:59"), checked_at=at("07:59")
        ).state
        == "unknown"
    )
    assert (
        validate_catchability(
            itinerary, origin, at("08:01"), at("07:59"), checked_at=at("07:59")
        ).state
        == "unknown"
    )


def test_unknown_coordinates_missing_times_and_partial_cancellation_fail_closed():
    from train_commute_companion.model import TimeObs

    connection = sample()
    walk, bus = connection.legs
    for bad in (
        replace(walk, part_cancelled=True),
        replace(walk, stops=(replace(walk.board, latitude=None, longitude=None), walk.alight)),
        replace(
            walk,
            stops=(
                walk.board,
                replace(
                    walk.alight,
                    arrival=TimeObs(
                        walk.alight.planned_arrival, None, Provenance.MISSING, at("07:59")
                    ),
                ),
            ),
        ),
        replace(
            walk,
            stops=(
                replace(
                    walk.board, departure=None, planned_departure=at("08:00").replace(tzinfo=None)
                ),
                walk.alight,
            ),
        ),
    ):
        with pytest.raises(RecoveryError):
            from_connection(replace(connection, legs=(bad, bus)))


def test_non_train_reference_does_not_read_train_id():
    from train_commute_companion.model import TrainId

    walk, bus = sample().legs
    bus = replace(
        bus,
        service_reference="provider-bus-12",
        train=TrainId(at("08:10").date(), "do-not-use-this-train-id", at("08:10")),
    )
    itinerary = from_connection(make_connection([walk, bus]))
    assert itinerary.steps[1].service_reference == "provider-bus-12"
    assert "provider-bus-12" in render(itinerary)
    assert "do-not-use-this-train-id" not in render(itinerary)


def test_provider_minimum_applies_between_vehicles_including_walk():
    train = replace(step("TRAIN", END, RAIL, "07:00", "08:00"), transfer_time_to_next=11)
    with pytest.raises(RecoveryError, match="minimum"):
        from_connection(make_connection([train, *sample().legs]))


def test_vehicle_change_uses_profile_minimum_without_double_counting_walk():
    train = step("TRAIN", END, RAIL, "07:00", "08:00")
    connection = make_connection([train, *sample().legs])
    itinerary = from_connection(connection, {"GD": 10})
    assert itinerary.transfers[0].minimum.total_seconds() == 600
    assert itinerary.transfers[0].walking.total_seconds() == 300
    assert itinerary.transfers[0].waiting.total_seconds() == 300
    with pytest.raises(RecoveryError, match="minimum"):
        from_connection(connection, {"GD": 11})


def test_vehicle_change_without_provider_or_profile_minimum_uses_fallback():
    from train_commute_companion.transfer import FALLBACK_MIN

    train = step("TRAIN", END, BUS, "07:00", "08:10")
    bus = sample().legs[1]
    with pytest.raises(RecoveryError, match="minimum"):
        from_connection(make_connection([train, bus]))
    later = replace(
        bus,
        stops=(
            replace(bus.board, departure=replace(bus.board.departure, value=at("08:15"))),
            bus.alight,
        ),
    )
    itinerary = from_connection(make_connection([train, later]))
    assert itinerary.transfers[0].minimum.total_seconds() == FALLBACK_MIN * 60


@pytest.mark.parametrize("limits", [{"GD": -1}, {"GD": True}, {"GD": 1.5}, {"GD": "4"}, {"": 4}])
def test_malformed_transfer_minimum_is_refused(limits):
    with pytest.raises(RecoveryError, match="minimum"):
        from_connection(sample(), limits)


def test_provider_refused_vehicle_change_is_not_a_recovery_option():
    train = replace(step("TRAIN", END, RAIL, "07:00", "08:00"), change_possible=False)
    with pytest.raises(RecoveryError, match="change"):
        from_connection(make_connection([train, *sample().legs]))


@pytest.mark.parametrize("mode", ["BUS", "METRO", "TRAM", "FERRY", "WALK"])
def test_non_train_modes_remain_distinct(mode):
    itinerary = from_connection(make_connection([step(mode, BUS, END, "08:10", "09:00")]))
    assert itinerary.steps[0].mode == mode


def test_outgoing_transfer_message_and_incoming_provider_time_set_minimum():
    train = replace(
        step("TRAIN", END, RAIL, "07:00", "08:00"), transfer_message_min=1, transfer_time_to_next=8
    )
    walk, bus = sample().legs
    bus = replace(bus, transfer_message_min=11)
    with pytest.raises(RecoveryError, match="minimum"):
        from_connection(make_connection([train, walk, bus]))
    bus = replace(bus, transfer_message_min=6)
    itinerary = from_connection(make_connection([train, walk, bus]))
    assert itinerary.transfers[0].minimum.total_seconds() == 8 * 60
    # The incoming leg's message describes boarding that leg, not this later transfer.
    train = replace(train, transfer_message_min=99)
    itinerary = from_connection(make_connection([train, walk, bus]))
    assert itinerary.transfers[0].minimum.total_seconds() == 8 * 60


def test_missing_provider_check_time_cannot_establish_catchability():
    itinerary = from_connection(sample())
    origin = itinerary.steps[0].origin
    assert validate_catchability(itinerary, origin, at("07:59"), at("07:59")).state == "unknown"


@pytest.mark.parametrize("status", ["CANCELLED", "UNKNOWN", "", "PARTIALLY_CANCELLED"])
def test_non_normal_trip_status_is_refused_even_with_valid_legs(status):
    with pytest.raises(RecoveryError, match="status"):
        from_connection(replace(sample(), status=status))


@pytest.mark.parametrize("checked_at", [None, at("07:54"), at("07:40"), at("08:00")])
def test_stale_future_and_missing_provider_times_cannot_establish_catchability(checked_at):
    itinerary = from_connection(sample())
    result = validate_catchability(
        itinerary, itinerary.steps[0].origin, at("07:59"), at("07:59"), checked_at=checked_at
    )
    assert result.state == "unknown"


def test_provider_timestamp_just_inside_freshness_boundary_allows_timetable_check():
    itinerary = from_connection(sample())
    result = validate_catchability(
        itinerary, itinerary.steps[0].origin, at("07:59"), at("07:59"), checked_at=at("07:54:01")
    )
    assert result.state == "feasible"

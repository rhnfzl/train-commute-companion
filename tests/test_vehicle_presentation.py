import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from train_commute_companion.calendar import TZ
from train_commute_companion.model import TrainId
from train_commute_companion.providers.ns import NsProvider
from train_commute_companion.vehicle import (
    PlatformFeature,
    SeatCapacity,
    StockUnit,
    TrainContext,
    VehiclePosition,
    VehicleProvider,
    VehicleSnapshot,
    VehicleWatch,
    render_snapshot,
    snapshot_payload,
)

NOW = datetime(2026, 11, 6, 21, 52, tzinfo=TZ)
TRAIN = TrainId(NOW.date(), "579", NOW)
WATCH = VehicleWatch(TRAIN, "UT", NOW - timedelta(seconds=10))
UNIT = StockUnit("8657", "VIRMm1 VI", ("TOILET", "WIFI"), SeatCapacity(120, 425, 3, 3, 6))
CONTEXT = TrainContext(
    TRAIN,
    "UT",
    NOW,
    "DVS",
    "VIRM",
    "NS",
    "12",
    False,
    6,
    161,
    (UNIT,),
    (PlatformFeature("PERRONLETTER", "M"), PlatformFeature("LIFT", None)),
    "right",
)
POSITION = VehiclePosition(
    "579", "579", 52.089043, 5.110265, "OBIS", "IC", 11.187, 149.81, 427.77777
)
SNAPSHOT = VehicleSnapshot(WATCH, CONTEXT, POSITION, NOW, NOW)


def test_shared_text_labels_retrieval_capacity_and_unverified_measurement_evidence():
    text = render_snapshot(SNAPSHOT)
    assert "service 579 at UT" in text and "service date unverified" in text
    assert "Context retrieved: 2026-11-06T21:52:00+01:00; source DVS" in text
    assert "carriages 6; length 161 m; shortened no" in text
    assert "not free-seat availability" in text and "second-class seats: 425" in text
    assert "toilet" in text and "Wi-Fi" in text and "Operation is unknown" in text
    assert "Platform 12" in text and "section M" in text and "lift" in text
    assert "Position candidate: 52.089043, 5.110265" in text
    assert "Measurement time is unknown" in text
    assert "does not confirm boarding or arrival" in text
    assert "11.187" not in text and "149.81" not in text and "427.77777" not in text
    assert "carriage 1" not in text and "pixel" not in text


def test_json_preserves_dated_journey_proof_sources_and_raw_values_with_unknown_units():
    fields = snapshot_payload(SNAPSHOT)
    assert fields["watch"]["train"]["operating_date"] == "2026-11-06"
    assert fields["watch"]["journey_checked_at"] == WATCH.journey_checked_at.isoformat()
    assert fields["position"]["reported_accuracy"] == 427.77777
    assert fields["position_is_candidate"]
    assert fields["coverage"] == "unknown" and fields["measurement_at"] is None
    assert fields["operating_date_confirmed"] is False
    assert fields["capacity_is_availability"] is False
    assert fields["equipment_operation_confirmed"] is False
    assert fields["measurement_time_known"] is False
    assert fields["boarding_confirmed"] is fields["arrival_confirmed"] is False
    assert fields["capacity_totals"]["second_class"] == 425
    assert "speed_unit" not in fields["position"]
    json.dumps(fields)


def test_totals_require_each_units_field_to_be_known_without_discarding_other_totals():
    second = replace(UNIT, capacity=SeatCapacity(None, 100, 0, 0, 0))
    snapshot = replace(SNAPSHOT, context=replace(CONTEXT, units=(UNIT, second)))
    fields = snapshot_payload(snapshot)
    assert fields["capacity_totals"]["first_class"] is None
    assert fields["capacity_totals"]["second_class"] == 525
    assert fields["capacity_totals"]["bicycle_spaces"] == 6
    text = render_snapshot(snapshot)
    assert "first-class seats: unknown" in text and "second-class seats: 525" in text


@pytest.mark.parametrize("units", [None, ()])
def test_missing_units_never_sum_to_fake_zero_capacity(units):
    snapshot = replace(SNAPSHOT, context=replace(CONTEXT, units=units))
    assert all(value is None for value in snapshot_payload(snapshot)["capacity_totals"].values())
    assert "first-class seats: unknown" in render_snapshot(snapshot)


def test_explicit_zero_capacity_survives_aggregation():
    unit = replace(UNIT, capacity=SeatCapacity(0, 0, 0, 0, 0))
    snapshot = replace(SNAPSHOT, context=replace(CONTEXT, units=(unit, unit)))
    assert set(snapshot_payload(snapshot)["capacity_totals"].values()) == {0}


@pytest.mark.parametrize("context,position", [(None, None), (None, POSITION), (CONTEXT, None)])
def test_unknown_or_hidden_stale_data_never_reappears_from_last_checked_fields(context, position):
    snapshot = replace(SNAPSHOT, context=context, position=position)
    text = render_snapshot(snapshot)
    fields = snapshot_payload(snapshot)
    if context is None:
        assert "context is unavailable or not current" in text
        assert "second-class seats: 425" not in text and fields["context"] is None
        assert "Last context retrieval" in text
    if position is None:
        assert "Vehicle position is unavailable or not current" in text
        assert "52.089043" not in text and fields["position"] is None
        assert not fields["position_is_candidate"]
        assert "Last position retrieval" in text


def test_no_retrievals_have_meaningful_unknown_text_without_fake_coordinates():
    snapshot = VehicleSnapshot(WATCH, None, None, None, None)
    text = render_snapshot(snapshot)
    assert "unavailable or not current" in text
    assert "0.000000" not in text and "Last position retrieval" not in text
    assert snapshot_payload(snapshot)["positions_checked_at"] is None


def test_vehicle_provider_can_be_detected_without_constructing_or_calling_it():
    assert isinstance(object.__new__(NsProvider), VehicleProvider)
    assert not isinstance(object(), VehicleProvider)

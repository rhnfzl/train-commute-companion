import json
from datetime import date, datetime, time

import pytest
from lifecycle_fixtures import L1, L2, RIDE, at, full_state, journey, occ

from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.lookahead_probe import LookAheadFact, LookAheadLedger
from train_commute_companion.serde import decode, dumps, loads
from train_commute_companion.store import PersistedState


def test_round_trip_every_persisted_type():
    st = full_state()
    back = loads(PersistedState, dumps(st))
    assert back == st
    occ_back = back.occurrences["k"]
    assert occ_back.zone_reports == {"UT": at("08:51")}
    assert occ_back.retained[0].legs[0].onward_from == at("08:44")
    assert back.config_snapshots[2].profiles[0].alerts.quiet_hours == (time(22), time(7))


def test_ledger_facts_stay_plain_lists_on_disk():
    stored = '{"facts": {"o": [["adopt:2", "moved", false, true]]}, "last_applied": null, '
    stored += '"last_run": null, "pending": [], "per_date": {}, "started_baseline": null, '
    stored += '"started_revision": 0}'
    led = loads(LookAheadLedger, stored)
    assert led.facts["o"] == (LookAheadFact("adopt:2", "moved", False, True),)
    assert led.facts["o"][0].warning and dumps(led) == stored


@pytest.mark.parametrize("bad", [["k", "t", False], ["k", "t", "no", False], "k"])
def test_a_bad_fact_raises_value_error(bad):
    with pytest.raises(ValueError):
        decode(LookAheadFact, bad)


def test_dst_fold_survives():
    from builders import local_at

    first, second = (
        local_at(date(2026, 10, 24), "02:30", 0),
        local_at(date(2026, 10, 24), "02:30", 1),
    )
    a, b = (loads(type(first), dumps(x)) for x in (first, second))
    assert a.utcoffset() != b.utcoffset()


def test_file_without_newer_fields_loads_with_defaults():
    raw = json.loads(dumps(occ(journey([L1, L2], RIDE))))
    del raw["zone_reports"]
    del raw["lock"]["legs"][0]["onward_from"]
    back = loads(OccurrenceState, json.dumps(raw))
    assert back.zone_reports == {} and back.lock.legs[0].onward_from is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(generation="x"),
        lambda d: d.update(extra=1),
        lambda d: d.pop("key"),
        lambda d: d.update(direction_state="nope"),
        lambda d: d["lock"].update(planned_final_arrival="2026-10-27T09:24:00"),  # naive
    ],
)
def test_bad_shapes_raise_value_error(mutate):
    raw = json.loads(dumps(occ(journey([L1, L2], RIDE))))
    mutate(raw)
    with pytest.raises(ValueError):
        loads(OccurrenceState, json.dumps(raw))


def test_old_files_load_without_copies():
    raw = json.loads(dumps(full_state()))
    del raw["webhook_copies"]
    for o in (*raw["obligations"], *raw["lookahead"]["p1"]["pending"]):
        del o["webhook_copied"]
    back = loads(PersistedState, json.dumps(raw))
    assert back.webhook_copies == ()
    assert [o.webhook_copied for o in back.obligations] == [False]
    assert [o.webhook_copied for o in back.lookahead["p1"].pending] == [False]


@pytest.mark.parametrize(
    "tp, v",
    [
        (datetime, "9999-12-31T22:30:00+00:00"),
        (datetime, "0001-01-02T00:00:00+00:00"),
        (date, "9999-12-31"),
        (date, "0001-01-01"),
    ],
)
def test_a_value_at_the_calendars_edge_is_a_value_error(tp, v):
    with pytest.raises(ValueError, match="expected"):
        decode(tp, v)


def test_a_datetime_out_of_range_in_our_zone_is_a_value_error():
    with pytest.raises(ValueError, match="expected"):
        decode(datetime, "0001-01-01T00:00:00+14:00")

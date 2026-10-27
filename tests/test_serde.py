import json
from dataclasses import replace
from datetime import date, time

import pytest
from lookahead_fixtures import NEXT_TUE, NOW, OUT, P, confirmed, ledger
from test_lifecycle_follow import L1, L2, RIDE, at, journey, occ

from train_commute_companion.delivery import Obligation
from train_commute_companion.health import PollRecord
from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.lookahead import BaselineHistory
from train_commute_companion.model import (
    AdviceRecord,
    AlertEvent,
    Config,
    EventKind,
    LocationSettings,
    Severity,
)
from train_commute_companion.serde import decode, dumps, encode, loads
from train_commute_companion.store import PersistedState


def full_state() -> PersistedState:
    """One of every persisted type, every optional field filled."""
    p = replace(
        P,
        extra_dates=frozenset({date(2026, 12, 24)}),
        location=LocationSettings("ha", "tracker", {"UT": "zone.ut"}),
        alerts=replace(
            P.alerts, latest_acceptable_arrival=time(10, 0), quiet_hours=(time(22), time(7))
        ),
    )
    cfg = Config(2, (p,))
    event = AlertEvent(
        "p1",
        "office_a",
        OUT,
        NEXT_TUE,
        1,
        EventKind.DELAYED,
        Severity.SILENT,
        L1.train,
        "t",
        "k",
        NOW,
    )
    ob = Obligation("o", event, None, ("p1", OUT, NEXT_TUE), 1, 3, "f", NOW, 2)
    retained = journey([L1, L2], RIDE)
    legs = (
        replace(retained.legs[0], onward_from=at("08:44"), accepted_at=at("08:50")),
        *retained.legs[1:],
    )
    s = occ(
        journey([L1, L2], RIDE),
        retained=(replace(retained, legs=legs),),
        cancellation_history=legs,
        alert_keys={"a": "b"},
        advice=AdviceRecord(L2.train, at("08:00"), False),
        poll_records=(PollRecord(at("08:00"), False, "rate"),),
        zone_reports={"UT": at("08:51")},
        tap_at=at("08:05"),
    )
    hist = BaselineHistory((confirmed(1), confirmed(2, date(2026, 11, 1))), floor=4)
    led = ledger(hist, per_date={"2026-11-03|j": ("a", "b")}, pending=(ob,), last_run=NOW)
    return PersistedState(
        cfg, {1: Config(1, ()), 2: cfg}, {"p1": hist}, {"p1": led}, {"k": s}, (ob,), True
    )


def test_round_trip_every_persisted_type():
    st = full_state()
    back = loads(PersistedState, dumps(st))
    assert back == st
    occ_back = back.occurrences["k"]
    assert occ_back.zone_reports == {"UT": at("08:51")}
    assert occ_back.retained[0].legs[0].onward_from == at("08:44")
    assert back.config_snapshots[2].profiles[0].alerts.quiet_hours == (time(22), time(7))


def test_dst_fold_survives():
    from builders import _at

    first, second = _at(date(2026, 10, 24), "02:30", 0), _at(date(2026, 10, 24), "02:30", 1)
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


def test_advice_record_saved_before_delivered_loads_as_told():
    raw = encode(AdviceRecord(L2.train, at("08:00"), False, delivered=False))
    del raw["delivered"]
    assert decode(AdviceRecord, raw).delivered is True
    assert decode(AdviceRecord, {**raw, "delivered": False}).delivered is False

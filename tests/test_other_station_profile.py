"""Profile schema and stored state of the other boarding station (0.3.0)."""

import json

from lifecycle_fixtures import L1, L2, journey, occ

from train_commute_companion.model import AlertFacts, Direction
from train_commute_companion.profile import parse_profiles
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import OccurrenceState
from train_commute_companion.store import PersistedState

STATIONS = frozenset({"AAA", "UT", "RTD", "VL"})
BASE = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "RTD"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: RET
    location: LOC
"""
LOC = "{source: ha, entity: person.me}"


def parse(ret: str, loc: str = LOC):
    return parse_profiles(BASE.replace("RET", ret).replace("LOC", loc), STATIONS)


def errors(ret: str, loc: str = LOC) -> list[tuple[str, str]]:
    return [(e.path.removeprefix("profiles[0]."), e.message) for e in parse(ret, loc)[1]]


def test_other_stations_and_location_settings_parse_with_defaults():
    (p,), errs = parse('{first_train: "17:52", buffer_min: 5, other_stations: [VL]}')
    assert errs == []
    assert p.journey(Direction.RETURN).other_stations == ("VL",)
    assert p.journey(Direction.OUTBOUND).other_stations == ()
    loc = p.location
    assert loc is not None
    assert (loc.other_station_radius_m, loc.other_station_dwell_min) == (500, 0)
    assert loc.ask_to_remember_station


def test_location_settings_are_read():
    loc = (
        "{source: ha, entity: person.me, other_station_radius_m: 300,"
        " other_station_dwell_min: 3, ask_to_remember_station: false}"
    )
    (p,), errs = parse('{first_train: "17:52", buffer_min: 5}', loc)
    assert errs == []
    assert p.location is not None
    assert (p.location.other_station_radius_m, p.location.other_station_dwell_min) == (300, 3)
    assert not p.location.ask_to_remember_station


def test_other_station_errors():
    assert errors('{first_train: "17:52", buffer_min: 5, other_stations: [VL, XX]}') == [
        ("return.other_stations[1]", "unknown station code 'XX'")
    ]
    ret = '{first_train: "17:52", buffer_min: 5, other_stations: [RTD, AAA]}'
    assert errors(ret) == [
        ("return.other_stations[0]", "RTD is already where this direction starts"),
        ("return.other_stations[1]", "AAA is where this direction ends"),
    ]
    assert errors('{first_train: "17:52", buffer_min: 5, other_stations: VL}') == [
        ("return.other_stations", "must be a list of station codes, [] for none")
    ]
    assert errors('{first_train: "17:52", buffer_min: 5, other_stations: [VL]}', "null") == [
        ("return.other_stations", "other_stations need a location block")
    ]


def test_location_setting_errors():
    loc = (
        "{source: ha, entity: person.me, other_station_radius_m: 0,"
        " other_station_dwell_min: 61, ask_to_remember_station: maybe}"
    )
    assert errors('{first_train: "17:52", buffer_min: 5}', loc) == [
        ("location.other_station_radius_m", "must be a whole number of metres from 1 to 5000"),
        ("location.other_station_dwell_min", "must be a whole number of minutes from 0 to 60"),
        ("location.ask_to_remember_station", "must be true or false, got 'maybe'"),
    ]


def test_state_from_0_2_6_loads_with_the_new_defaults():
    s = occ(journey([L1, L2]))
    raw = json.loads(json.dumps(encode(s)))
    for k in ("boarding_station", "prompt", "asked_stations", "station_seen"):
        del raw[k]  # 0.2.6 had no such fields
    assert decode(OccurrenceState, raw) == s
    st = PersistedState(None, {}, {}, {}, {}, (), False)
    raw = json.loads(json.dumps(encode(st)))
    del raw["remembered_stations"]
    assert decode(PersistedState, raw).remembered_stations == {}
    facts = json.loads(json.dumps(encode(AlertFacts())))
    del facts["prompt_id"], facts["choices"]
    assert decode(AlertFacts, facts) == AlertFacts()

"""A profile may name a station instead of giving its code; the parsed profile holds codes."""

import json
from datetime import date
from pathlib import Path

import pytest
from runtime_rig import PROFILE, Rig, StationsProvider, kinds, started

from train_commute_companion.model import EventKind, Stations
from train_commute_companion.profile import load_config, parse_profiles
from train_commute_companion.providers.base import ProviderError
from train_commute_companion.providers.ns import map_station_index, map_stations

ROOT = Path(__file__).parent.parent
# a few real NS stations: neutral ones plus the names NS gives to more than one station
KNOWN = map_station_index(json.loads((ROOT / "tests/data/ns/stations_names.json").read_text()))
TODAY = date(2026, 10, 26)
EXAMPLE = (ROOT / "examples/profiles.yaml").read_text()

ONE = """
profiles:
  - id: "p1"
    name: "office_a"
    days: [tue]
    home_station: HOME
    work_station: "RTD"
    outbound: {first_train: "08:12", buffer_min: 10, transfers: TRANSFERS}
    return: {first_train: "17:52", buffer_min: 5}
    transfer_min: MINIMUM
"""


def text(home: str = '"GV"', transfers: str = '["UT"]', minimum: str = "{UT: 4}") -> str:
    return ONE.replace("HOME", home).replace("TRANSFERS", transfers).replace("MINIMUM", minimum)


def parse(t: str, known: frozenset[str] | None = KNOWN):
    profiles, errors = parse_profiles(t, known)
    return profiles, [f"{e.path}: {e.message} ({e.suggestion})" for e in errors]


def test_the_ns_index_has_every_name_and_synonym():
    assert KNOWN == map_stations(
        json.loads((ROOT / "tests/data/ns/stations_names.json").read_text())
    )
    assert KNOWN.name("UT") == "Utrecht Centraal"
    assert {KNOWN.lookup(n) for n in ("Utrecht C.", "Utrecht CS", "utrecht")} == {("UT",)}
    assert KNOWN.lookup("Den Haag Hollands Spoor") == ("GV",)


@pytest.mark.parametrize(
    ("written", "code"),
    [
        ('"GV"', "GV"),  # a code is taken as it is
        ('"Den Haag HS"', "GV"),  # the long name
        ('"den haag hs"', "GV"),  # case does not matter
        ('"Hollands Spoor"', "GV"),  # an NS synonym
        ('"Köln Hbf"', "KOLN"),
        ('"Koln Hbf."', "KOLN"),
        ('"Bunde (Westf)"', "BUENDE"),  # written without the accent NS uses
    ],
)
def test_a_station_name_becomes_its_code(written, code):
    profiles, errors = parse(text(home=written))
    assert errors == [] and profiles[0].home_station == code


def test_a_code_wins_over_a_name_that_spells_it():
    known = Stations({"UT": ["Utrecht Centraal"], "RTD": [], "GV": [], "XX": ["UT"]})
    profiles, errors = parse(text(), known)
    assert errors == [] and profiles[0].outbound.transfers == ("UT",)


def test_accents_decide_before_they_are_ignored():
    known = Stations({"A": ["Bünde"], "B": ["Bunde"]})
    assert known.lookup("Bünde") == ("A",) and known.lookup("BUNDE") == ("B",)
    assert Stations({"A": ["Bünde"]}).lookup("bunde") == ("A",)


def test_a_name_of_more_than_one_station_is_refused_with_their_codes():
    _, errors = parse(text(home='"Bunde"'))
    assert errors == [
        (
            "profiles[0].home_station: 'Bunde' names more than one station: BDE (Bunde),"
            " BUENDE (Bünde (Westf)), BUNDE (Bunde (D)) (use the station code)"
        )
    ]
    _, errors = parse(text(home='"Berlin Hbf"'))
    assert "BERHBL" in errors[0] and "BERHBT" in errors[0]


def test_an_unknown_name_gets_a_close_suggestion_never_a_guess():
    _, errors = parse(text(home='"Utrecht Centrall"'))
    assert errors == [
        (
            "profiles[0].home_station: unknown station code 'Utrecht Centrall'"
            " (did you mean 'Utrecht Centraal'?)"
        )
    ]
    _, errors = parse(text(home='"gv"'))
    assert errors[0].endswith("(did you mean 'GV'?)")
    _, errors = parse(text(home='"Nowhere At All"'))
    assert errors[0].endswith("(use a code from the NS station list)")


@pytest.mark.parametrize(
    ("transfers", "minimum"),
    [
        ('["Utrecht Centraal"]', "{UT: 4}"),
        ('["UT"]', '{"Utrecht Centraal": 4}'),
        ('["Utrecht"]', '{"Utrecht C.": 4}'),
    ],
)
def test_transfer_minimums_match_by_station_not_by_spelling(transfers, minimum):
    profiles, errors = parse(text(transfers=transfers, minimum=minimum))
    assert errors == []
    assert profiles[0].outbound.transfers == ("UT",) and profiles[0].transfer_min == {"UT": 4}


def test_two_minimums_for_one_station_are_refused():
    _, errors = parse(text(minimum='{UT: 4, "Utrecht Centraal": 5}'))
    assert errors == [
        (
            "profiles[0].transfer_min.Utrecht Centraal: Utrecht Centraal and UT are the same"
            " station UT (keep one of them)"
        )
    ]


def test_a_missing_minimum_is_still_reported_as_written():
    _, errors = parse(text(transfers='["Utrecht Centraal"]', minimum="{}"))
    assert errors[0].startswith(
        "profiles[0].transfer_min.Utrecht Centraal: no transfer_min for transfer station"
    )


def test_unchecked_a_spare_minimum_may_be_the_transfer_by_another_name():
    assert parse(text(transfers='["Utrecht Centraal"]'), None)[1] == []  # waits for the list
    _, errors = parse(text(transfers='["Utrecht Centraal"]', minimum="{}"), None)
    assert errors[0].startswith("profiles[0].transfer_min.Utrecht Centraal: no transfer_min")


def test_names_and_codes_give_the_same_configuration():
    codes, _ = load_config(text(), KNOWN, None, TODAY)
    assert codes is not None
    named = text(home='"Den Haag HS"', transfers='["Utrecht"]', minimum='{"Utrecht CS": 4}')
    assert load_config(named, KNOWN, codes, TODAY) == (codes, [])


def test_the_starter_file_parses_the_same_with_names_known():
    codes = frozenset({"AAA", "UT", "RTD"})
    named = Stations({"AAA": ["Aaa"], "UT": ["Utrecht Centraal"], "RTD": ["Rotterdam Centraal"]})
    plain = load_config(EXAMPLE, codes, None, TODAY)
    assert plain[0] is not None and plain[1] == []
    assert load_config(EXAMPLE, named, None, TODAY) == plain
    assert load_config(EXAMPLE, None, None, TODAY) == plain


NAMED = PROFILE.replace("transfers: [UT]", 'transfers: ["Utrecht Centraal"]')
RIG_KNOWN = Stations({"AAA": [], "UT": ["Utrecht Centraal"], "RTD": []})


async def test_a_name_never_reaches_a_query_while_the_station_list_is_down(tmp_path):
    rig = Rig(tmp_path)
    rig.write(text=NAMED.replace("NAME", "office_a"))
    rt = rig.rt = rig.new(require_stations=True)

    async def down() -> frozenset[str]:
        raise ProviderError("station endpoint down")

    rig.provider.stations = down  # type: ignore[method-assign]
    await rt.start()
    await rig.run_until("09:30")
    assert rt.config is None and rig.provider.calls == []
    assert rt.refusal is not None and "station list is unavailable" in rt.refusal


async def test_a_named_profile_runs_on_codes(tmp_path):
    rig = Rig(tmp_path)
    rig.write(text=NAMED.replace("NAME", "office_a"))
    rig.provider = StationsProvider(rig.provider, RIG_KNOWN)  # type: ignore[assignment]
    rig.rt = rig.new(require_stations=True)
    await rig.rt.start()
    await rig.run_until("08:00")
    assert rig.rt.config is not None
    assert rig.rt.config.profiles[0].outbound.transfers == ("UT",)
    calls = rig.provider.inner.calls  # type: ignore[attr-defined]
    stations = {s for q in calls for s in (q.origin, q.destination, q.via)} - {None}
    assert calls and stations <= {"AAA", "UT", "RTD"}


async def test_a_named_file_waits_after_a_restart_while_the_saved_codes_keep_running(tmp_path):
    rig = await started(tmp_path)  # the codes configuration is stored
    revision = rig.rt.config.revision
    rig.write(text=NAMED.replace("NAME", "office_a"))
    rt = rig.rt = rig.new(require_stations=True)
    up = False

    async def stations() -> frozenset[str]:
        if not up:
            raise ProviderError("station endpoint down")
        return RIG_KNOWN

    rig.provider.stations = stations  # type: ignore[method-assign]
    await rt.start()
    await rig.run_until("08:00")
    assert rt.config is not None and rt.config.profiles[0].outbound.transfers == ("UT",)
    assert rig.provider.calls
    assert {s for q in rig.provider.calls for s in (q.origin, q.destination, q.via)} <= {
        "AAA",
        "UT",
        "RTD",
        None,
    }
    assert kinds(rt.events_log).count(EventKind.PROFILE_REFUSED) == 1
    up = True
    rig.clock.jump(2)
    await rt.reload_if_changed()
    assert rt.refusal is None and rt.config.revision == revision

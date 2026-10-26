"""Look-ahead edge cases not covered elsewhere: local winter times, announcements, health."""

from lookahead_fixtures import (
    DAYS,
    MON,
    NEXT_TUE,
    TUE,
    WED,
    P,
    probe,
    run,
)
from test_lookahead_rows import HISTORY, week

from train_commute_companion.lookahead import (
    encode,
    probe_dates,
    signature_of,
)
from train_commute_companion.model import EventKind


def test_winter_trips_are_read_in_local_time():
    rs = probe(TUE, HISTORY.in_force(TUE))  # after the autumn change: +01:00
    assert rs and all(r.fingerprint not in ("", "no_service") for r in rs)
    assert all(not r.works for r in rs)
    whole = next(r for r in rs if r.leg_index is None)
    assert encode(signature_of(whole.connection, TUE)).startswith("AAA>UT@08:12+0/28")


def test_a_three_minute_shift_on_one_day_only_is_quiet():
    shifted = {"l1": ("08:15", "08:43"), "l2": ("08:49", "09:27")}
    h, _, events = run(HISTORY, week(**{WED.isoformat(): shifted}))
    assert h == HISTORY and events == []


def test_a_healthy_run_records_success():
    _, led, events = run(HISTORY, week())
    assert led.last_run is not None and events == []


def test_one_failed_horizon_still_records_success():
    rs = [r for r in week() if r.service_date != WED]  # Wednesday's answers never came
    _, led, _ = run(HISTORY, rs)
    assert led.last_run is not None


def test_only_commute_days_are_probed():
    # the profile commutes Tue and Wed: the next three are never fixed +7 / +14 offsets
    assert probe_dates(P, MON) == [TUE, WED, NEXT_TUE]


def test_one_surviving_probe_never_looks_complete():
    shifted = {"l1": ("08:15", "08:43"), "l2": ("08:49", "09:27")}
    rs = [r for r in week(**{d.isoformat(): shifted for d in DAYS}) if r.service_date == TUE]
    h, _, events = run(HISTORY, rs)
    assert h == HISTORY and not any(e.kind is EventKind.TIMETABLE_ADOPTED for e in events)

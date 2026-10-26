from dataclasses import replace
from datetime import date, time, timedelta

from builders import make_connection, make_leg
from lookahead_fixtures import (
    CLEAR,
    DAYS,
    MON,
    NEXT_TUE,
    NOW,
    OUT,
    TUE,
    UNKNOWN,
    WED,
    P,
    ans,
    confirmed,
    first_train,
    ledger,
    probe,
    profile,
    run,
)

from train_commute_companion.delivery import Obligation
from train_commute_companion.lookahead import (
    BaselineHistory,
    BaselineVersion,
    evaluate,
    probe_dates,
    rebase_on_edit,
    run_lookahead,
)
from train_commute_companion.model import AlertEvent, EventKind, Severity

SHIFTED = {"l1": ("08:15", "08:43"), "l2": ("08:49", "09:27")}


def probes(history: BaselineHistory, per_day=None, **kw):
    per_day = per_day or {}
    return [
        r for day in DAYS for r in probe(day, history.in_force(day), **(per_day.get(day) or kw))
    ]


def test_works_on_second_leg_only():  # spec fixture
    rs = probe(TUE, cancel2=True)
    by_leg = {r.leg_index: r for r in rs}
    assert not by_leg[0].works and by_leg[1].works and by_leg[None].works
    _, _, events = run(BaselineHistory(), rs)
    (ev,) = events
    assert ev.kind is EventKind.WORKS_AHEAD and ev.severity is Severity.LOUD  # tomorrow
    assert ev.text.startswith("Office A, Tuesday 27 Oct: ")
    assert "your UT to RTD leg has no trains (works)" in ev.text
    assert "AAA to UT" not in ev.text
    assert ev.text.endswith("Check replacement transport nearer the day.")


def test_return_day_offset_service_date_11_days_out_not_probed():
    ps = profile()
    sat = date(2026, 10, 31)  # commute days Tue 3 Nov (+3) and Tue 10 Nov (+10)
    tue_only = replace(ps, days=frozenset({2}))
    same_day = replace(tue_only, return_=replace(ps.return_, first_train=time(4, 0)))
    next_day = replace(same_day, return_=replace(same_day.return_, day_offset=1))
    assert probe_dates(same_day, sat) == [date(2026, 11, 3), date(2026, 11, 10)]
    assert probe_dates(next_day, sat) == [date(2026, 11, 3)]  # its return is on 11 Nov, +11


def test_failed_maintenance_lookup_no_clean_probe():
    rs = probe(TUE, works=UNKNOWN)
    assert rs and not any(r.clean for r in rs) and not any(r.works for r in rs)
    history, led, events = run(BaselineHistory(), rs)
    assert history.versions == () and events == []
    assert led.last_run is None  # counts as a failed run in look-ahead health


def test_planned_transfer_below_minimum_alerts_and_seeds_nothing():
    rs = probe(TUE, l2=("08:43", "09:24"))  # 3 min at UT, minimum 4
    assert all(not r.transfer_ok and not r.clean for r in rs)
    history, _, events = run(BaselineHistory(), rs)
    assert history.versions == ()
    (ev,) = events
    assert ev.text.count("your change at UT is too short (3 min, you need 4)") == 1


def test_cold_start_seed_then_confirm_on_other_day():
    first = probe(TUE) + probe(WED, cancel2=True) + probe(NEXT_TUE, cancel1=True)
    h1, led1, _ = run(BaselineHistory(), first)
    (v1,) = h1.versions
    assert not v1.confirmed and v1.signature.legs[0].departure == time(8, 12)
    # next day's run: the same timetable on another commute day confirms it, silently
    second = probe(WED, v1) + probe(NEXT_TUE, v1, cancel1=True)
    h2, _, events = run(h1, second, led=led1, today=TUE)
    assert [v.confirmed for v in h2.versions] == [True]
    assert h2.versions[0].version == 1
    assert all(e.kind is not EventKind.TIMETABLE_ADOPTED for e in events)


def test_unanimous_adoption_one_silent_message_file_unchanged():  # Q2 fixtures
    history = BaselineHistory((confirmed(),))
    h, led, events = run(history, probes(history, **SHIFTED))
    (ev,) = events
    assert ev.kind is EventKind.TIMETABLE_ADOPTED and ev.severity is Severity.SILENT
    assert "NS moved your 08:12 to 08:15 from Tuesday 27 Oct" in ev.text
    assert first_train(h, MON) == time(8, 12) and first_train(h, TUE) == time(8, 15)
    assert h.versions[-1].confirmed and h.versions[-1].effective_from == TUE
    assert P.outbound.first_train == time(8, 12)  # the user's file is never rewritten
    assert len(led.pending) == 1 and led.pending[0].baseline_version == 2


def test_non_unanimous_adopts_nothing():
    history = BaselineHistory((confirmed(),))
    rs = probes(history, per_day={TUE: SHIFTED, WED: SHIFTED, NEXT_TUE: {}})
    h, _, events = run(history, rs)
    assert h == history and events == []  # a 3-min shift on some days is below tolerance


def _adoption_obligation(version: int) -> Obligation:
    ev = AlertEvent(
        "p1", "office_a", OUT, NEXT_TUE, None, EventKind.TIMETABLE_ADOPTED, Severity.SILENT,
        None, "x", "k", MON,
    )  # fmt: skip
    return Obligation("o", ev, None, ("p1", OUT, NEXT_TUE), 1, version, "adopt", None, 0)


def test_waiting_version_cancelled_before_date_never_activates():
    waiting = BaselineVersion(2, confirmed(**SHIFTED).signature, NEXT_TUE, True)
    history = BaselineHistory((confirmed(), waiting))
    led = ledger(history, pending=(_adoption_obligation(2),))
    h, led2, events = run(history, probes(history), led=led)
    assert [v.version for v in h.versions] == [1]
    assert first_train(h, NEXT_TUE) == time(8, 12)
    assert led2.pending == () and events == []  # the untold adoption is just dropped


def test_waiting_version_already_told_gets_a_silent_restored():
    waiting = BaselineVersion(2, confirmed(**SHIFTED).signature, NEXT_TUE, True)
    history = BaselineHistory((confirmed(), waiting))
    _, _, events = run(history, probes(history))
    (ev,) = events
    assert ev.kind is EventKind.RESTORED and ev.severity is Severity.SILENT
    assert ev.service_date == NEXT_TUE and "stays as it was" in ev.text


def test_run_after_route_edit_writes_nothing():
    history = BaselineHistory((confirmed(),))
    led = ledger(history, revision=1)  # the run started on revision 1
    assert run_lookahead(P, OUT, history, led, probes(history), 2, MON, NOW) is None


def test_two_leg_warnings_tomorrow_one_message_both_facts():
    rs = probe(TUE, cancel1=True, cancel2=True)
    _, led, events = run(BaselineHistory(), rs)
    (ev,) = events
    assert ev.severity is Severity.LOUD
    assert "your AAA to UT leg has no trains" in ev.text
    assert "your UT to RTD leg has no trains" in ev.text
    assert len(led.pending) == 1 and led.pending[0].look_ahead == ("p1", OUT, TUE)


def _detour(day: date):
    a = make_leg("AAA", "BBB", "08:12", "08:50", day=day, number="3000")
    b = make_leg("BBB", "RTD", "08:55", "09:30", day=day, number="4000")
    return [ans("AAA", "UT"), ans("UT", "RTD"), ans("AAA", "RTD", make_connection([a, b]))]


def test_pattern_change_never_adopted_profile_refused():
    history = BaselineHistory((confirmed(),))
    rs = [r for d in DAYS for r in evaluate(P, OUT, d, _detour(d), CLEAR, history.in_force(d))]
    h, led, events = run(history, rs)
    assert h == history
    kinds = [e.kind for e in events]
    assert kinds.count(EventKind.WORKS_AHEAD) == 3
    refused = [e for e in events if e.kind is EventKind.PROFILE_REFUSED]
    assert len(refused) == 1 and refused[0].severity is Severity.LOUD
    assert "goes via BBB (works)" in events[0].text
    # while it lasts, the refusal is told again; the per-date works are not repeated
    _, _, again = run(h, rs, led=ledger(h, per_date=led.per_date))
    assert [e.kind for e in again] == [EventKind.PROFILE_REFUSED]


def test_window_edit_after_adoption_keeps_baseline():
    history = BaselineHistory((confirmed(), confirmed(2, TUE, **SHIFTED)))
    edited = profile(window=(time(7, 0), time(10, 0)))
    assert rebase_on_edit(P, edited, OUT, history, WED) is history


def test_cancelled_version_number_is_never_reused():
    waiting = BaselineVersion(2, confirmed(**SHIFTED).signature, NEXT_TUE, True)
    history = BaselineHistory((confirmed(), waiting))
    h, _, _ = run(history, probes(history))
    assert [v.version for v in h.versions] == [1] and h.floor == 2
    # the same shift seen again is adopted under a fresh number, so a run started under the
    # cancelled v2 cannot pass the baseline check against it
    h2, _, _ = run(h, probes(h, **SHIFTED), led=ledger(h))
    assert h2.latest == 3
    assert run_lookahead(P, OUT, h2, ledger(history), probes(h2), 1, MON, NOW) is None


def test_stored_ledger_with_start_stamp_survives_a_route_edit():
    history = BaselineHistory((confirmed(),))
    _, stored, _ = run(history, probes(history), now=NOW - timedelta(days=1))
    # the profile moved to revision 2 since; this run started on it
    started = replace(stored, started_revision=2, started_baseline=history.latest)
    assert run_lookahead(P, OUT, history, started, probes(history), 2, MON, NOW) is not None
    # a newer run that finished first wins
    newer = replace(started, last_run=NOW + timedelta(minutes=5))
    assert run_lookahead(P, OUT, history, newer, probes(history), 2, MON, NOW) is None

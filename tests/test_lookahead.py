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
    legs,
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
    reset_on_edit,
    run_lookahead,
)
from train_commute_companion.model import AlertEvent, Direction, EventKind, Severity

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


# ---- review fixes


def _answers(a, b, leg2_status="NORMAL", extra=()):
    return [
        ans("AAA", "UT", make_connection([a])),
        ans("UT", "RTD", make_connection([b], status=leg2_status)),
        ans("AAA", "RTD", make_connection([a, b]), *extra),
    ]


def test_silent_works_warning_escalates_once_when_tomorrow():  # rule 4
    h = BaselineHistory()
    _, led, events = run(h, probe(WED, cancel2=True))
    assert [e.severity for e in events] == [Severity.SILENT]
    tue = NOW + timedelta(days=1)
    stored = ledger(h, per_date=led.per_date)
    _, led2, again = run(h, probe(WED, cancel2=True), led=stored, today=TUE, now=tue)
    (ev,) = again
    assert ev.severity is Severity.LOUD and "UT to RTD leg has no trains" in ev.text
    stored = ledger(h, per_date=led2.per_date)
    _, _, third = run(h, probe(WED, cancel2=True), led=stored, today=TUE, now=tue)
    assert third == []  # escalated once only


def test_reported_delay_does_not_hide_short_planned_transfer():
    a, b = (
        make_leg("AAA", "UT", "08:12", "08:40", day=TUE, number="1000"),
        make_leg("UT", "RTD", "08:43", "09:24", day=TUE, number="2000", delay_dep=2),
    )
    rs = evaluate(P, OUT, TUE, _answers(a, b), CLEAR, None)
    assert rs and all(not r.transfer_ok and not r.clean for r in rs)
    history, _, events = run(BaselineHistory(), rs)
    assert history.versions == ()
    assert "your change at UT is too short (3 min, you need 4)" in events[0].text


def test_newer_adoption_retires_superseded_waiting_version():
    waiting = BaselineVersion(2, confirmed(l1=("08:19", "08:47")).signature, NEXT_TUE, True)
    history = BaselineHistory((confirmed(), waiting))
    led = ledger(history, pending=(_adoption_obligation(2),))
    h, led2, _ = run(history, probes(history, **SHIFTED), led=led)
    assert [v.version for v in h.versions] == [1, 3] and h.floor >= 2
    assert first_train(h, NEXT_TUE) == time(8, 15)  # never reverts to the waiting 08:19
    assert all(o.baseline_version != 2 for o in led2.pending)


def test_failed_health_run_still_blocks_an_older_overlapping_run():  # rule 21
    h = BaselineHistory()
    later = NOW + timedelta(minutes=5)
    _, led, events = run(h, probe(TUE, works=UNKNOWN, cancel2=True), now=later)
    assert events and led.last_run is None  # failed health, but its writes were applied
    assert run_lookahead(P, OUT, h, led, probe(TUE), 1, MON, NOW) is None


def test_whole_journey_duration_increase_is_a_change():
    history = BaselineHistory((confirmed(),))
    longer = {"l2": ("08:50", "09:37")}  # leg 2: +4 min departure, +9 min duration
    rs = probes(history, per_day={TUE: longer, WED: {}, NEXT_TUE: {}})
    h, _, events = run(history, rs)
    assert h == history
    (ev,) = events
    assert ev.service_date == TUE and ev.kind is EventKind.WORKS_AHEAD
    assert "your journey changes to 08:12, 85 min (was 08:12, 72 min)" in ev.text


def test_adoption_checks_overlap_with_sibling_direction_adoption():  # rule 19, Q2
    p = replace(
        P, return_=replace(P.return_, first_train=time(10, 52), window=(time(10, 27), time(12, 0)))
    )
    ten = {"l1": ("08:22", "08:50"), "l2": ("08:56", "09:34")}
    history = BaselineHistory((confirmed(),))
    rs = [r for day in DAYS for r in probe(day, history.in_force(day), p=p, **ten)]
    back = confirmed(l1=("10:42", "11:10"), l2=("11:16", "11:54"))  # return moved -10
    bases = {("p1", Direction.RETURN): BaselineHistory((back,))}
    alone = run_lookahead(p, OUT, history, ledger(history), rs, 1, MON, NOW)
    assert alone is not None and alone[0].latest == 2  # +10 alone fits
    out = run_lookahead(p, OUT, history, ledger(history), rs, 1, MON, NOW, bases=bases)
    assert out is not None
    h, _, events = out
    assert h == history
    assert any(e.kind is EventKind.PROFILE_REFUSED and "overlaps" in e.text for e in events)


def test_route_edit_resets_warning_dedup():  # rule 10
    h = BaselineHistory((confirmed(),))
    _, led, events = run(h, probe(TUE, h.in_force(TUE), cancel2=True))
    assert events
    moved = profile(first_train=time(8, 20))
    reseeded = rebase_on_edit(P, moved, OUT, h, MON)
    fresh = reset_on_edit(P, moved, OUT, h, led, MON)
    assert fresh.per_date == {}
    _, _, again = run(reseeded, probe(TUE, cancel2=True), led=replace(fresh, started_baseline=None))
    assert again  # told again for the new route
    window = profile(window=(time(7, 0), time(10, 0)))
    assert reset_on_edit(P, window, OUT, h, led, MON) is led


def test_maintenance_on_a_leg_probe_blocks_seed_and_confirmation():
    rs = []
    for day in (TUE, WED):
        a, b = legs(day)
        rs += evaluate(P, OUT, day, _answers(a, b, leg2_status="MAINTENANCE"), CLEAR, None)
    assert any(r.works for r in rs if r.leg_index == 1)
    h, _, _ = run(BaselineHistory(), rs)
    assert h.versions == ()


def test_whole_journey_tie_break_keeps_the_destination():
    a, b = legs(TUE)
    wrong = make_leg("UT", "BBB", "08:46", "09:10", day=TUE, number="5000")
    rtd = make_connection([a, b], status="MAINTENANCE")
    answers = [ans("AAA", "UT", make_connection([a])), ans("UT", "RTD", make_connection([b]))]
    answers.append(ans("AAA", "RTD", make_connection([a, wrong]), rtd))
    (whole,) = [r for r in evaluate(P, OUT, TUE, answers, CLEAR, None) if r.leg_index is None]
    assert whole.connection is not None and whole.connection.destination == "RTD"
    assert whole.works and not whole.clean

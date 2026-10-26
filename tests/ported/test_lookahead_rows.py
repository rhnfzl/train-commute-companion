"""Look-ahead edge cases (prior-art section 4) by behaviour, with timetable signatures."""

from dataclasses import replace
from datetime import date, time

from builders import make_connection, make_leg
from lookahead_fixtures import (
    CLEAR,
    DAYS,
    MON,
    NEXT_TUE,
    OUT,
    TUE,
    WED,
    P,
    ans,
    confirmed,
    day_answers,
    first_train,
    ledger,
    probe,
    profile,
    run,
)

from train_commute_companion.lookahead import (
    BaselineHistory,
    evaluate,
    probe_dates,
    probe_queries,
)
from train_commute_companion.model import EventKind

HISTORY = BaselineHistory((confirmed(),))


def week(**per_day):
    return [
        r for d in DAYS for r in probe(d, HISTORY.in_force(d), **per_day.get(d.isoformat(), {}))
    ]


def test_queries_carry_the_offset_of_the_probed_date_not_the_run_date():
    assert probe_queries(P, OUT, TUE, None)[0].at.utcoffset().total_seconds() == 3600
    summer = probe_queries(P, OUT, date(2026, 6, 2), None)[0].at
    assert summer.utcoffset().total_seconds() == 7200 and summer.strftime("%H:%M") == "07:57"


def test_every_train_cancelled_is_no_trains_not_missing():
    _, _, (ev,) = run(HISTORY, probe(TUE, HISTORY.in_force(TUE), cancel1=True, cancel2=True))
    assert "has no trains (works)" in ev.text and "no usual train" not in ev.text


def test_cut_short_train_is_never_no_trains():
    rs = probe(TUE, HISTORY.in_force(TUE), stops2=("BBB",), cut2="BBB")
    _, _, (ev,) = run(HISTORY, rs)
    assert "runs only to BBB" in ev.text and "no trains" not in ev.text


def test_missing_answer_is_not_an_observation_and_not_warned():
    rs = evaluate(P, OUT, TUE, [], CLEAR, HISTORY.in_force(TUE))
    assert rs == []
    _, led, events = run(HISTORY, rs)
    assert events == [] and led.last_run is None


def test_mispaired_answer_is_ignored():
    wrong = [ans("RTD", "AAA", *day_answers(TUE)[2].connections)]
    assert evaluate(P, OUT, TUE, wrong, CLEAR, HISTORY.in_force(TUE)) == []


def test_one_cancelled_day_among_normal_days_warns_that_date_only():
    h, _, events = run(HISTORY, week(**{WED.isoformat(): {"cancel2": True}}))
    assert h == HISTORY
    assert [e.service_date for e in events] == [WED]


def test_every_day_cancelled_warns_and_never_adopts_no_trains():
    h, _, events = run(HISTORY, week(**{d.isoformat(): {"cancel1": True} for d in DAYS}))
    assert h == HISTORY and len(events) == 3
    assert all(e.kind is EventKind.WORKS_AHEAD for e in events)


def test_told_change_is_not_repeated_and_restored_once_and_relapse_warns_again():
    broken = week(**{WED.isoformat(): {"cancel2": True}})
    _, led, _ = run(HISTORY, broken)
    _, led, again = run(HISTORY, broken, led=ledger(HISTORY, per_date=led.per_date))
    assert again == []
    _, led, restored = run(HISTORY, week(), led=ledger(HISTORY, per_date=led.per_date))
    assert [e.kind for e in restored] == [EventKind.RESTORED]
    assert "back to normal" in restored[0].text
    _, led, quiet = run(HISTORY, week(), led=ledger(HISTORY, per_date=led.per_date))
    assert quiet == []
    _, _, relapse = run(HISTORY, broken, led=ledger(HISTORY, per_date=led.per_date))
    assert [e.service_date for e in relapse] == [WED]


def test_unanimous_small_shift_is_adopted_from_the_soonest_date():
    shifted = {"l1": ("08:15", "08:43"), "l2": ("08:49", "09:27")}
    h, _, (ev,) = run(HISTORY, week(**{d.isoformat(): shifted for d in DAYS}))
    assert ev.kind is EventKind.TIMETABLE_ADOPTED and ev.service_date == TUE
    assert first_train(h, MON) == time(8, 12)  # waits for its date
    assert first_train(h, TUE) == time(8, 15)


def test_unanimous_change_already_announced_is_adopted_without_restored():
    late = {"l1": ("08:22", "08:50"), "l2": ("08:56", "09:34")}  # 10 min: warned per day first
    _, led, warned = run(HISTORY, week(**{TUE.isoformat(): late}))
    assert [e.kind for e in warned] == [EventKind.WORKS_AHEAD]
    h, _, events = run(
        HISTORY,
        week(**{d.isoformat(): late for d in DAYS}),
        led=ledger(HISTORY, per_date=led.per_date),
    )
    assert [e.kind for e in events] == [EventKind.TIMETABLE_ADOPTED]
    assert first_train(h, TUE) == time(8, 22)


def test_public_holiday_is_not_a_probe_day():
    every_day = replace(P, days=frozenset(range(1, 8)))
    days = probe_dates(every_day, date(2026, 12, 23))  # Fri 25 and Sat 26 Dec are holidays
    assert date(2026, 12, 25) not in days and len(days) == 3


def test_direct_train_becoming_a_via_train_is_works():
    direct = profile(transfers=())
    a = make_leg("AAA", "UT", "08:12", "08:40", day=TUE, number="1000")
    b = make_leg("UT", "RTD", "08:46", "09:24", day=TUE, number="2000")
    rs = evaluate(direct, OUT, TUE, [ans("AAA", "RTD", make_connection([a, b]))], CLEAR, None)
    (r,) = rs
    assert r.works and r.fingerprint.startswith("pattern:")


def test_no_service_day_still_counts_as_a_successful_run():
    _, led, _ = run(HISTORY, week(**{d.isoformat(): {"cancel1": True} for d in DAYS}))
    assert led.last_run is not None


def test_cold_start_without_baseline_still_warns_works_but_not_changes():
    late = {"l1": ("08:22", "08:50"), "l2": ("08:56", "09:34")}
    _, _, events = run(BaselineHistory(), probe(NEXT_TUE, **late))
    assert events == []  # no confirmed baseline: a different time is not a change yet

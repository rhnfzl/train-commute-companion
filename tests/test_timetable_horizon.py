import asyncio
from dataclasses import replace
from datetime import date, time, timedelta

import pytest
from lookahead_fixtures import CLEAR, MON, NOW, OUT, TUE, UNKNOWN, YAML, confirmed, day_answers
from runtime_rig import DAY, Rig, at, every_day

from train_commute_companion.lookahead import run_lookahead
from train_commute_companion.lookahead_probe import (
    evaluate,
    probe_dates,
    probe_queries,
    service_date,
)
from train_commute_companion.lookahead_run import apply_run, directions_due, probe_direction
from train_commute_companion.model import (
    AlertSettings,
    Config,
    DayOverride,
    Direction,
    EventKind,
    OccurrenceKey,
    direction_key,
    okey,
)
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import PlannedWork, PlannedWorks, RateLimited
from train_commute_companion.runtime import fresh_state
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import BaselineHistory, LookAheadLedger


def configured(unit, count, *, enabled=True):
    profiles, errors = parse_profiles(
        YAML
        + f"""
    alerts:
      custom_timetable_horizon: {str(enabled).lower()}
      timetable_horizon_unit: {unit}
      timetable_horizon_count: {count}
""",
        None,
    )
    assert not errors
    return profiles[0]


def test_default_checks_all_five_weekdays_starting_tomorrow():
    profiles, errors = parse_profiles(YAML, None)
    assert not errors
    p = replace(profiles[0], days=frozenset(range(1, 6)))
    plan = probe_dates(p, MON, direction=Direction.OUTBOUND)
    assert plan.complete
    assert plan.dates == tuple(MON + timedelta(days=n) for n in (1, 2, 3, 4, 7))
    assert decode(AlertSettings, {}) == p.alerts


@pytest.mark.parametrize("sparse, expected", [(False, 3), (True, 2)])
def test_switch_off_restores_legacy_count_and_calendar_limit(sparse, expected):
    p = configured("commute_dates", 14, enabled=False)
    assert decode(AlertSettings, encode(p.alerts)) == p.alerts
    p = replace(p, days=frozenset({2} if sparse else range(1, 6)))
    plan = probe_dates(p, MON, direction=OUT)
    assert plan.complete and len(plan.dates) == expected
    assert plan.dates[-1] <= MON + timedelta(days=10)


@pytest.mark.parametrize("count, dates", [(7, 5), (10, 8), (14, 10)])
@pytest.mark.parametrize("sparse", [False, True])
@pytest.mark.parametrize("unit", ["calendar_days", "commute_dates"])
async def test_date_and_trip_request_counts(tmp_path, unit, sparse, count, dates):
    p = configured(unit, count)
    p = replace(
        p,
        days=frozenset({2} if sparse else range(1, 6)),
        return_=replace(p.return_, transfers=()),
    )
    expected = count if unit == "commute_dates" else ((count + 6) // 7 if sparse else dates)
    plans = {d: probe_dates(p, MON, direction=d).dates for d in Direction}
    assert all(len(days) == expected for days in plans.values())
    assert (
        sum(len(probe_queries(p, d, day, None)) for d, days in plans.items() for day in days)
        == expected * 4
    )
    assert all(day > MON for days in plans.values() for day in days)
    if sparse and unit == "commute_dates":
        assert plans[Direction.OUTBOUND][-1] == MON + timedelta(days=1 + (count - 1) * 7)
    st = replace(Rig(tmp_path).rt.state, config=Config(1, (p,)))
    trips = []

    async def fetch(queries):
        trips.extend(queries)
        return ()

    for direction, days in plans.items():
        trips.clear()
        run = await probe_direction(st, p, direction, CLEAR, NOW, fetch)
        assert run is not None and not run.results
        assert trips == [q for day in days for q in probe_queries(p, direction, day, None)]
        assert len(set(trips)) == len(trips)


@pytest.mark.parametrize(
    "field, value",
    [
        ("timetable_horizon_count", value)
        for value in ("true", "false", "0", "-1", "15", "7.5", "7.0", "'7'", "null")
    ]
    + [("timetable_horizon_unit", value) for value in ("months", "null", "[]")]
    + [("custom_timetable_horizon", value) for value in ("7", "'true'", "null")],
)
def test_reject_invalid_horizon_settings(field, value):
    profiles, errors = parse_profiles(YAML + f"    alerts: {{{field}: {value}}}\n", None)
    assert not profiles and any(f"alerts.{field}" in str(error) for error in errors)


@pytest.mark.parametrize(
    "unit, expected", [("calendar_days", (1, 6, 7)), ("commute_dates", (1, 6, 7, 9, 10, 11, 13))]
)
def test_commute_selection_observes_holidays_pauses_skips_and_window_days(unit, expected):
    today = date(2026, 12, 24)
    p = configured(unit, 7)
    p = replace(
        p,
        days=frozenset(range(1, 8)),
        extra_dates=frozenset({date(2026, 12, 25)}),
        pauses=((date(2026, 12, 27), date(2026, 12, 27)),),
        outbound=replace(p.outbound, by_weekday={"tue": DayOverride(mode="window")}),
    )
    plan = probe_dates(p, today, direction=OUT, skipped={date(2026, 12, 28)})
    assert plan.complete
    # Christmas Day is an extra date. Boxing Day, Sunday, Monday and Tuesday are excluded.
    assert plan.dates == tuple(today + timedelta(days=n) for n in expected)


def test_commute_search_is_bounded_and_reports_partial_coverage():
    p = configured("commute_dates", 14)
    p = replace(p, pauses=((TUE + timedelta(days=1), MON + timedelta(days=370)),))
    partial = probe_dates(p, MON, direction=OUT)
    assert partial.dates == (TUE,)
    assert not partial.complete
    for unit in ("calendar_days", "commute_dates"):
        edge = probe_dates(configured(unit, 14), date.max, direction=OUT)
        assert not edge.complete and not edge.dates


def test_overnight_return_keeps_commute_day_at_horizon_end_across_dst():
    today = date(2027, 3, 20)
    last = date(2027, 3, 27)
    p = configured("calendar_days", 7)
    p = replace(
        p, days=frozenset({6}), return_=replace(p.return_, day_offset=1, first_train=time(2, 15))
    )
    back = Direction.RETURN
    assert probe_dates(p, today, direction=back).dates == (last,)
    service = date(2027, 3, 28)
    assert service_date(p, back, last) == service
    assert probe_queries(p, back, last, None)[0].at.isoformat() == "2027-03-28T01:45:00+01:00"
    assert probe_dates(p, today, direction=back, skipped={service}).dates == ()
    assert probe_dates(p, today, direction=OUT, skipped={service}).dates == (last,)


def test_transfer_after_midnight_keeps_its_actual_departure_date():
    today = date(2026, 11, 1)
    last = today + timedelta(days=7)
    p = replace(configured("calendar_days", 7), days=frozenset({7}))
    base = confirmed()
    first, onward = base.signature.legs
    base = replace(
        base,
        signature=replace(
            base.signature,
            legs=(
                replace(first, departure=time(23, 30)),
                replace(onward, departure=time(0, 30), day_offset=1),
            ),
        ),
    )
    assert probe_dates(p, today, direction=OUT).dates == (last,)
    assert [q.at.date() for q in probe_queries(p, OUT, last, base)] == [
        last,
        last + timedelta(days=1),
        last,
    ]


@pytest.mark.parametrize(
    "problem", [None, "missing_date", "empty", "off_date", "failed_works", "works", "cancelled"]
)
def test_timetable_adoption_needs_agreement_and_valid_horizon_evidence(problem):
    p = configured("calendar_days", 14)
    base = confirmed()
    history = BaselineHistory((base,))
    dates = probe_dates(p, MON, direction=OUT).dates
    results = []
    shifted = {"l1": ("08:15", "08:43"), "l2": ("08:49", "09:27")}
    for day in dates:
        works = CLEAR
        answers = day_answers(day, **shifted)
        if day == dates[-1]:
            if problem == "missing_date":
                continue
            if problem == "empty":
                answers = [replace(answer, connections=()) for answer in answers]
            if problem == "off_date":
                answers = day_answers(day + timedelta(days=1), **shifted)
            if problem == "failed_works":
                works = UNKNOWN
            if problem == "works":
                start = NOW + (day - MON)
                works = PlannedWorks(
                    (PlannedWork(start, start + timedelta(days=1), frozenset(), "Work"),),
                    "complete",
                )
            if problem == "cancelled":
                answers = day_answers(day, **shifted, cancel2=True)
        results += evaluate(p, OUT, day, answers, works, base)
    out = run_lookahead(
        p,
        OUT,
        history,
        LookAheadLedger({}, (), None, 1, 1),
        results,
        revision=1,
        today=MON,
        now=NOW,
        others=(),
        bases={},
    )
    assert out is not None
    new_history, ledger, events = out
    assert (new_history != history) is (problem is None)
    assert any(e.kind is EventKind.TIMETABLE_ADOPTED for e in events) is (problem is None)
    assert all("no usual train" not in e.text for e in events)
    if problem in ("missing_date", "empty", "off_date", "failed_works"):
        assert ledger.last_run is None and ledger.last_applied == NOW
    if problem == "cancelled":
        assert any("has no trains" in e.text for e in events)


async def test_probe_direction_applies_skip_and_keeps_warning_before_failed_query(tmp_path):
    p = configured("commute_dates", 7)
    rig = Rig(tmp_path, "05:40")
    st = replace(rig.rt.state, config=Config(1, (p,)))
    skip = OccurrenceKey(p.id, OUT, TUE)
    st = replace(
        st, occurrences={okey(skip): replace(fresh_state(skip, 1, None), not_travelling=True)}
    )
    asked = []

    async def fetch(queries):
        day = queries[0].at.date()
        asked.append(day)
        if len(asked) == 2:
            return "provider"
        return tuple(day_answers(day, cancel2=True))

    run = await probe_direction(st, p, OUT, CLEAR, NOW, fetch)
    assert run is not None
    assert asked == [TUE + timedelta(days=1), TUE + timedelta(days=7)]
    changed = apply_run(st, rig.rt._queue, p, OUT, run)
    assert changed is not None
    ledger = changed.lookahead[direction_key(p.id, OUT)]
    assert ledger.last_run is None and ledger.last_applied == NOW
    assert len(ledger.pending) == 1 and ledger.pending[0].event.kind is EventKind.WORKS_AHEAD
    assert (p.id, OUT) not in directions_due(changed, NOW)


async def test_new_skip_does_not_make_a_failed_probe_proof_for_adoption(tmp_path):
    p = configured("calendar_days", 7)
    rig = Rig(tmp_path, "05:40")
    key = direction_key(p.id, OUT)
    history = BaselineHistory((confirmed(),))
    st = replace(rig.rt.state, config=Config(1, (p,)), baselines={key: history})
    wed = TUE + timedelta(days=1)

    async def fetch(queries):
        if queries[0].at.date() == wed:
            return "provider"
        return tuple(day_answers(TUE, l1=("08:15", "08:43"), l2=("08:49", "09:27")))

    run = await probe_direction(st, p, OUT, CLEAR, NOW, fetch)
    assert run is not None and run.results and not run.probe_finished
    skip = OccurrenceKey(p.id, OUT, wed)
    edited = replace(
        st, occurrences={okey(skip): replace(fresh_state(skip, 1, 1), not_travelling=True)}
    )
    changed = apply_run(edited, rig.rt._queue, p, OUT, run)
    assert changed is not None
    assert changed.baselines[key] == history and changed.lookahead[key].last_run is None


async def test_custom_background_pacing_leaves_live_poll_free_and_obeys_backoff(tmp_path):
    rig = Rig(tmp_path, "07:50")
    rig.provider.world = every_day()
    await rig.rt.start()
    wait_started, resume = asyncio.Event(), asyncio.Event()
    sleep = rig.clock.sleep
    future_calls = []

    async def wait(seconds):
        wait_started.set()
        await resume.wait()
        await sleep(seconds)

    def answer(query):
        if query.at.date() > DAY:
            future_calls.append(rig.clock.now())
            if len(future_calls) == 3:
                return RateLimited()
        return None

    rig.clock.sleep = wait
    rig.provider.error = answer
    background = asyncio.create_task(rig.rt.lookahead_once())
    try:
        await asyncio.wait_for(wait_started.wait(), 1)
        assert len(future_calls) == 1
        before = len(rig.provider.calls)
        await asyncio.wait_for(rig.rt.poll_once(), 1)
        assert len(rig.provider.calls) > before
        assert len(future_calls) == 1
    finally:
        resume.set()
        await asyncio.wait_for(background, 1)
    assert future_calls == [at("07:50") + timedelta(seconds=n) for n in (0, 2, 4)]

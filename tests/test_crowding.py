import json
from dataclasses import replace
from pathlib import Path

import pytest
from builders import make_leg
from lifecycle_fixtures import (
    L1,
    L1X,
    L2,
    L2X,
    LATER,
    R2,
    RIDE,
    ans,
    at,
    conn,
    ctx,
    journey,
    occ,
    poll,
    usual_locked,
)
from runtime_rig import Rig, started

from train_commute_companion.classify import current_run
from train_commute_companion.ledger import LiveLedger, PollMemory
from train_commute_companion.messages import all_good_text
from train_commute_companion.model import Config, EventKind, LegState, Obligation
from train_commute_companion.providers.base import Query
from train_commute_companion.providers.ns import map_trips
from train_commute_companion.serde import dumps, loads
from train_commute_companion.store import PersistedState
from train_commute_companion.view import occurrence_view


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("LOW", "low"),
        ("MEDIUM", "medium"),
        ("HIGH", "high"),
        ("UNKNOWN", None),
        (None, None),
        ({}, None),
    ],
)
def test_provider_preserves_crowd_forecast_without_treating_unknown_as_quiet(raw, expected):
    data = json.loads((Path(__file__).parent / "data/ns/on_time_direct.json").read_text())
    data["trips"][0]["legs"][0]["crowdForecast"] = raw
    answer = map_trips(data, Query("AAA", "BBB", at("07:50")), at("07:50"))
    assert answer.connections[0].legs[0].crowd_forecast == expected


def test_forecast_reaches_view_and_message_and_survives_storage():
    leg = replace(L1, crowd_forecast="high")
    context = ctx(at("07:50"))
    answer = ans("AAA", conn(leg, L2))
    view = occurrence_view(usual_locked(), context, [answer])
    assert view.legs[0].crowd_forecast == "high"
    assert "crowd forecast: high per NS" in all_good_text([leg])
    assert loads(type(leg), dumps(leg)).crowd_forecast == "high"


@pytest.mark.parametrize("old,new", [("high", None), (None, "high"), ("high", "low")])
def test_newest_forecast_wins_even_when_new_copy_ends_before_the_transfer(old, new):
    full = replace(L1, crowd_forecast=old)
    short = replace(
        L1, stops=(L1.board, replace(L1.alight, code="MID", name="Mid")), crowd_forecast=new
    )
    result = current_run(
        [ans("AAA", conn(full, L2), when="07:50"), ans("AAA", conn(short), when="07:52")],
        L1.train,
        "AAA",
        "UT",
    )
    assert result[1].leg.crowd_forecast == new


@pytest.mark.parametrize("levels", [("low", "high"), ("high", "low"), (None, "low")])
def test_conflicting_forecasts_at_the_same_fetch_are_unknown(levels):
    answer = ans("AAA", *(conn(replace(L1, crowd_forecast=level), L2) for level in levels))
    result = current_run([answer], L1.train, "AAA", "UT")
    assert result[1].leg.crowd_forecast is None


def test_high_forecast_is_silent_news_before_boarding():
    leg = replace(L1, crowd_forecast="high")
    result = poll(usual_locked(), ctx(at("07:50")), ans("AAA", conn(leg, L2)))
    events = [e for e in result.events if e.kind.value == "crowd_change"]
    assert len(events) == 1
    assert events[0].severity.value == "silent"
    assert "high crowd forecast per NS" in events[0].text


def test_unknown_forecast_does_not_send_crowd_news():
    result = poll(usual_locked(), ctx(at("07:50")), ans("AAA", conn(L1, L2)))
    assert all(e.kind.value != "crowd_change" for e in result.events)


async def test_changes_are_delivered_once_and_remembered_after_restart(tmp_path):
    rig = Rig(tmp_path, "08:04")
    rig.provider.world = [conn(replace(L1, crowd_forecast="low"), L2)]
    await rig.rt.start()
    await rig.rt.poll_once()
    assert not [e for e in rig.notifier.attempts if e.kind is EventKind.CROWD_CHANGE]
    for when, level in [("08:06", "high"), ("08:07", "high"), ("08:08", "medium")]:
        rig.clock.t = at(when)
        rig.provider.world = [conn(replace(L1, crowd_forecast=level), L2)]
        await rig.rt.poll_once()
    await rig.restart()
    await rig.rt.poll_once()
    events = [e for e in rig.notifier.attempts if e.kind is EventKind.CROWD_CHANGE]
    assert [e.facts.crowd_forecast for e in events] == ["high", "medium"]


async def test_failed_crowd_alert_retries_without_repeating_after_delivery(tmp_path):
    rig = Rig(tmp_path, "08:04")
    rig.provider.world = [conn(replace(L1, crowd_forecast="high"), L2)]
    rig.notifier.fails = lambda e: e.kind is EventKind.CROWD_CHANGE
    await rig.rt.start()
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.CROWD_CHANGE]) == 1
    rig.notifier.fails = lambda e: False
    rig.clock.t = at("08:06")
    await rig.rt.poll_once()
    count = len([e for e in rig.notifier.attempts if e.kind is EventKind.CROWD_CHANGE])
    assert count == 2
    await rig.rt.poll_once()
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.CROWD_CHANGE]) == count


def test_departed_train_does_not_send_crowd_news():
    state = usual_locked()
    state = replace(
        state,
        lock=replace(
            state.lock,
            legs=(replace(state.lock.legs[0], state=LegState.RIDING), *state.lock.legs[1:]),
        ),
    )
    result = poll(state, ctx(at("08:20")), ans("AAA", conn(replace(L1, crowd_forecast="high"), L2)))
    assert all(e.kind.value != "crowd_change" for e in result.events)


@pytest.mark.parametrize("current", ["low", None])
async def test_delayed_retry_refreshes_only_crowd_clause(tmp_path, current):
    rig = await started(tmp_path, "08:04")
    delayed = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=5)
    rig.notifier.fails = lambda e: e.kind is EventKind.DELAYED
    rig.clock.t = at("08:06")
    rig.provider.world = [conn(replace(delayed, crowd_forecast="high"), L2)]
    await rig.rt.poll_once()
    first = [e for e in rig.notifier.attempts if e.kind is EventKind.DELAYED][-1]
    assert "crowd forecast: high per NS" in first.text
    rig.notifier.fails = lambda e: False
    rig.clock.t = at("08:08")
    rig.provider.world = [conn(replace(delayed, crowd_forecast=current), L2)]
    await rig.rt.poll_once()
    retry = [e for e in rig.notifier.attempts if e.kind is EventKind.DELAYED][-1]
    assert retry.key == first.key and retry.severity == first.severity
    assert "high per NS" not in retry.text
    assert (
        ("crowd forecast: low per NS" in retry.text)
        if current
        else ("crowd forecast" not in retry.text)
    )


async def test_cancelled_retry_refreshes_same_replacement_forecasts(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.clock.t = at("07:52")
    rig.provider.world = [
        conn(L1X, L2),
        replace(LATER, legs=tuple(replace(x, crowd_forecast="high") for x in LATER.legs)),
    ]
    await rig.rt.poll_once()
    first = [e for e in rig.notifier.attempts if e.kind is EventKind.CANCELLED][-1]
    assert "crowd forecasts per NS" in first.text and "high" in first.text
    rig.notifier.fails = lambda e: False
    rig.clock.t = at("07:54")
    rig.provider.world = [
        conn(L1X, L2),
        replace(LATER, legs=tuple(replace(x, crowd_forecast="low") for x in LATER.legs)),
    ]
    await rig.rt.poll_once()
    retry = [e for e in rig.notifier.attempts if e.kind is EventKind.CANCELLED][-1]
    assert retry.key == first.key and retry.severity == first.severity
    assert "high" not in retry.text and "AAA-UT low, UT-RTD low" in retry.text


@pytest.mark.parametrize("current", ["low", None])
def test_retained_warning_refreshes_its_own_replacement_not_the_main_lock(current):
    state = occ(journey(LATER.legs), retained=(journey([L1, L2], RIDE),))
    context = ctx(at("08:20"))
    original = ans("AAA", conn(L1, L2X), LATER, when="08:20")
    previous = ans("UT", conn(replace(R2, crowd_forecast="high")), when="08:20")
    result = poll(state, context, original, previous)
    event = next(e for e in result.events if e.kind is EventKind.CANCELLED)
    assert "crowd forecast: high" in event.text and event.facts.on
    obligation = Obligation("x", event, result.state.key, None, 1, None, event.key, None, 0)
    config = Config(1, (context.profile,))
    key = "p1|outbound|2026-10-27"
    stored = PersistedState(config, {1: config}, {}, {}, {key: result.state}, (obligation,), True)
    latest = ans("UT", conn(replace(R2, crowd_forecast=current)), when="08:21")
    ledger = LiveLedger(
        lambda: stored,
        lambda: stored,
        PollMemory(boards={key: (original, latest)}),
        lambda: at("08:21"),
    )
    refreshed = ledger.current(obligation)
    assert "high" not in refreshed.text
    assert "if you are on the 08:12" in refreshed.text and "09:05" in refreshed.text
    assert (
        ("crowd forecast: low per NS" in refreshed.text)
        if current
        else ("crowd forecast" not in refreshed.text)
    )

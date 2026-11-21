from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import runtime_seam as seam
from builders import cut_stop, make_connection, make_leg, planned_at_alight
from lifecycle_fixtures import ans, poll
from runtime_rig import DAY, Rig, at
from test_window_mode import (
    FIXED_OUT,
    PROFILE,
    RET_KEY,
    STAY_RET,
    commute_day,
    first_train,
    report,
    ride_to_work,
    rig_for,
    told,
)
from test_window_profile import parse

from train_commute_companion.calendar import TZ
from train_commute_companion.lifecycle import left_note, merged_pool
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import Direction, EventKind, LegState, Obligation, Severity
from train_commute_companion.providers.base import ProviderError, Query
from train_commute_companion.revisions import occurrence_ctx
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.serde import decode, encode
from train_commute_companion.state import StationPrompt
from train_commute_companion.view import occurrence_view

WINDOW = 'mode: window, window: ["07:00", "09:00"], buffer_min: 10, pick: arrive_by'
SETTINGS = f'{{{WINDOW}, arrive_by: "09:00", walk_after_min: 10}}'


def direct(dep, arr, n="1", **kwargs):
    return make_connection([make_leg("AAA", "RTD", dep, arr, number=n, **kwargs)])


def change(dep="08:20", via="UT"):
    return make_connection(
        [
            make_leg("AAA", via, dep, "08:28", number="20"),
            make_leg(via, "RTD", "08:38", "08:48", number="21"),
        ]
    )


@pytest.mark.parametrize(
    ("margin", "direct_at", "expected"),
    [
        (None, "08:05", "08:05"),
        (None, "08:04", "08:20"),
        (0, "08:05", "08:20"),
        (5, "08:15", "08:15"),
    ],
)
async def test_direct_preference_uses_an_inclusive_departure_margin(
    tmp_path, margin, direct_at, expected
):
    settings = SETTINGS if margin is None else SETTINGS[:-1] + f", prefer_direct_min: {margin}}}"
    rig = await rig_for(tmp_path, out=settings, world=[direct(direct_at, "08:49"), change()])
    await rig.run_until("06:37")
    assert first_train(rig) == expected
    assert len(rig.provider.calls) == 1
    (query,) = rig.provider.calls
    assert query.search_for_arrival and query.at == at("08:50")
    assert not rig.occ().advice.active


async def test_arrival_board_is_filtered_for_deadline_and_evidence(tmp_path):
    planned = planned_at_alight(direct("08:38", "08:48", "3").legs[0])
    world = [
        direct("08:42", "08:51", "4"),  # NS can return an arrival after the target
        direct("08:40", "08:52", "5", delay_arr=-4),
        make_connection([planned]),
        direct("08:31", "08:49", "2", delay_arr=2),
        direct("08:23", "08:50"),
    ]
    rig = await rig_for(tmp_path, out=SETTINGS, world=world)
    await rig.run_until("06:37")
    assert first_train(rig) == "08:23"
    (occ,) = rig.rt.view().occurrences
    assert occ.arrive_by == at("09:00")
    assert occ.station_arrival_deadline == at("08:50")
    assert occ.arrival_target_status == "on_target"
    assert occ.legs[0].train_number == "1"


async def test_cannot_catch_the_only_on_target_train(tmp_path):
    rig = await rig_for(
        tmp_path,
        out=SETTINGS,
        now="08:15",
        world=[
            direct("08:24", "08:50"),
            direct("08:30", "08:51", "2"),
        ],
    )
    await rig.run_until("08:19")
    assert first_train(rig) is None
    assert rig.rt.view().occurrences[0].arrival_target_status == "no_train"
    assert told(rig, EventKind.ARRIVAL_TARGET) == [
        ("no train you can catch was found for arrival by 09:00, check the NS app.", Severity.LOUD),
    ]
    assert [e.kind for e in rig.notifier.attempts] == [EventKind.ARRIVAL_TARGET]


async def test_required_transfers_and_accessibility_scope(tmp_path):
    settings = SETTINGS[:-1] + ", transfers: [UT]}"
    rig = await rig_for(
        tmp_path,
        out=settings,
        world=[direct("08:19", "08:49"), change()],
        extra="    accessibility: {require_accessible: true}\n    transfer_min: {UT: 5}\n",
    )
    await rig.run_until("06:37")
    assert first_train(rig) == "08:20"
    assert rig.occ().lock.legs[0].alight_stop == "UT"
    assert rig.provider.calls[0].via == "UT"
    assert rig.provider.calls[0].require_accessible
    rig.provider.world = [direct("08:22", "08:47"), change(via="GDA")]
    await rig.run_until("06:39")
    assert first_train(rig) == "08:20"


@pytest.mark.parametrize("failure", ["cancelled", "late", "unknown"])
async def test_switches_only_to_an_earlier_train_when_the_lock_fails(tmp_path, failure):
    earlier = direct("08:05", "08:46", "1")
    selected = direct("08:25", "08:49", "2")
    later = direct("08:27", "08:49", "3")
    rig = await rig_for(tmp_path, out=SETTINGS, world=[earlier, selected])
    await rig.run_until("06:37")
    assert first_train(rig) == "08:25"
    fail = {
        "cancelled": direct("08:25", "08:49", "2", cancelled=True),
        "late": direct("08:25", "08:49", "2", delay_arr=2),
        "unknown": make_connection([planned_at_alight(selected.legs[0])]),
    }[failure]
    rig.provider.world = [earlier, later, fail]
    await rig.run_until("06:39")
    assert first_train(rig) == "08:05"
    event_kind = EventKind.CANCELLED if failure == "cancelled" else EventKind.ARRIVAL_TARGET
    ((message, severity),) = told(rig, event_kind)
    assert severity is Severity.LOUD
    assert "earlier train at 08:05" in message
    assert any(
        e.kind is event_kind and "earlier train at 08:05" in e.body for e in rig.notifier.attempts
    )
    assert not told(rig, EventKind.LEAVE_NOW)
    assert [q.search_for_arrival for q in rig.provider.calls] == [True, False, True, False]
    assert rig.provider.calls[-1].at == at("08:05")
    rig.provider.world = [earlier, selected, later]
    await rig.run_until("06:41")
    assert first_train(rig) == "08:05"


@pytest.mark.parametrize("delay", [{"delay_arr": 6}, {"delay_dep": 21}])
async def test_arrive_by_recheck_withholds_replacement_that_breaks_its_limits(tmp_path, delay):
    earlier = direct("08:05", "08:46", "1")
    selected = direct("08:25", "08:49", "2")
    rig = await rig_for(tmp_path, out=SETTINGS, world=[earlier, selected])
    await rig.run_until("06:37")
    assert first_train(rig) == "08:25"
    cancelled = direct("08:25", "08:49", "2", cancelled=True)
    rig.provider.world = [earlier, cancelled]

    def reply(q):
        if q.origin == "AAA" and q.at == at("08:05"):
            rig.provider.world = [direct("08:05", "08:46", "1", **delay), cancelled]

    rig.provider.hook = reply
    await rig.run_until("06:39")
    ((message, _),) = told(rig, EventKind.CANCELLED)
    assert "08:05" not in message
    assert "08:26" not in message
    assert "Check the NS app" in message


async def test_arrive_by_rechecks_an_earlier_train_after_midnight(tmp_path):
    earlier = direct("00:05", "00:40", "1")
    selected = direct("00:25", "00:44", "2")
    settings = SETTINGS.replace('"07:00", "09:00"', '"23:30", "00:45"').replace(
        'arrive_by: "09:00"', 'arrive_by: "01:00"'
    )
    rig = await rig_for(tmp_path, out=settings, world=[earlier, selected], now="23:34")
    await rig.run_until("23:37")
    assert first_train(rig) == "00:25"
    rig.provider.world = [earlier, direct("00:25", "00:44", "2", cancelled=True)]
    await rig.run_until("23:39")
    ((message, _),) = told(rig, EventKind.CANCELLED)
    assert "earlier train at 00:05" in message
    assert "NS checked" in message


async def test_late_lock_does_not_move_to_a_later_train_or_hide_loud_limit(tmp_path):
    rig = await rig_for(
        tmp_path,
        out=SETTINGS,
        world=[direct("08:25", "08:49")],
        extra=('    alerts: {latest_acceptable_arrival: "08:50", delay_min: 10}\n'),
    )
    await rig.run_until("06:37")
    rig.provider.world = [
        direct("08:25", "08:49", delay_arr=2),
        direct("08:27", "08:49", "2"),
    ]
    await rig.run_until("06:39")
    assert first_train(rig) == "08:25"
    assert rig.rt.view().occurrences[0].arrival_target_status == "late"
    assert "No earlier train you can catch" in told(rig, EventKind.ARRIVAL_TARGET)[0][0]
    assert told(rig, EventKind.DELAYED)[0][1] is Severity.LOUD
    assert not told(rig, EventKind.LEAVE_NOW)


async def test_arrival_target_alert_keeps_the_independent_delay_audience(tmp_path):
    rig = await rig_for(
        tmp_path,
        out=SETTINGS,
        world=[direct("08:25", "08:49")],
        extra="    alerts: {disabled_categories: [journey]}\n",
    )
    await rig.run_until("06:37")
    rig.provider.world = [direct("08:25", "08:49", delay_arr=4)]
    await rig.run_until("06:39")
    sent = {e.kind for e in rig.notifier.attempts}
    assert EventKind.DELAYED in sent
    assert EventKind.ARRIVAL_TARGET not in sent


async def test_a_failed_earlier_search_preserves_the_primary_cancellation(tmp_path):
    rig = await rig_for(tmp_path, out=SETTINGS, world=[direct("08:25", "08:49")])
    await rig.run_until("06:37")
    rig.provider.world = [direct("08:25", "08:49", cancelled=True)]
    rig.provider.error = lambda q: (
        ProviderError("arrival search unavailable") if q.search_for_arrival else None
    )
    await rig.run_until("06:39")
    assert first_train(rig) == "08:25"
    assert "is cancelled" in told(rig, EventKind.CANCELLED)[0][0]
    assert rig.occ().poll_records[-1].ok


@pytest.mark.parametrize(
    ("arrival_delay", "required", "expected"),
    [(15, False, ["1", "2"]), (None, False, ["1", "2"]), (0, True, ["1", "2"]), (0, False, ["1"])],
    ids=["late", "unconfirmed-arrival", "missing-required-change", "eligible"],
)
async def test_short_turn_recovery_stays_unless_restored_route_meets_plan(
    tmp_path, arrival_delay, required, expected
):
    incoming = make_leg("AAA", "UT", "08:15", "08:33", number="1")
    through = make_leg("UT", "RTD", "08:34", "08:49", number="1", delay_arr=arrival_delay or 0)
    if arrival_delay is None:
        through = planned_at_alight(through)
    transfer = replace(
        incoming.alight,
        planned_departure=through.board.planned_departure,
        departure=through.board.departure,
    )
    direct_leg = direct("08:15", "08:49").legs[0]
    train = replace(direct_leg, stops=(incoming.board, transfer, direct_leg.alight))
    onward = make_leg("UT", "RTD", "08:40", "08:49", number="2")
    rig = await rig_for(
        tmp_path,
        out=SETTINGS,
        now="08:00",
        world=[make_connection([train])],
        extra="    transfer_min: {UT: 4}\n",
    )
    await rig.run_until("08:18")
    cut = replace(train, stops=(*train.stops[:-1], cut_stop(train.alight)), part_cancelled=True)
    rig.provider.world = [make_connection([cut]), make_connection([onward])]
    await rig.run_until("08:24")
    assert [x.train.train_number for x in rig.occ().lock.legs] == ["1", "2"]
    assert rig.occ().lock.legs[0].alight_stop == "UT"

    state = rig.occ()
    ctx = occurrence_ctx(rig.rt.state, state, at("08:26"))
    if required:
        profile = ctx.profile
        ctx = replace(
            ctx, profile=replace(profile, outbound=replace(profile.outbound, transfers=("UT",)))
        )
    restored = replace(train, stops=(*train.stops[:-1], through.alight))
    retained_pair = make_connection([incoming, replace(onward, transfer_message_min=5)])
    other_incoming = make_leg("AAA", "UT", "08:16", "08:33", number="77")
    unrelated_pair = make_connection([other_incoming, replace(onward, transfer_message_min=12)])
    answers = (
        ans("AAA", retained_pair, when="08:25"),
        ans("AAA", make_connection([restored]), when="08:26"),
        ans("AAA", unrelated_pair, when="08:26"),
    )
    followed = poll(state, ctx, *answers)
    assert [x.train.train_number for x in followed.state.lock.legs] == expected
    assert followed.state.lock.legs[0].alight_stop == ("UT" if len(expected) == 2 else "RTD")
    if len(expected) == 2:
        assert followed.state.lock.legs[0].onward_from == at("08:38")
    assert any(e.kind is EventKind.RESTORED for e in followed.events) == (len(expected) == 1)
    assert occurrence_view(followed.state, ctx, answers).arrival_target_status == "on_target"


async def test_reachable_later_onward_train_keeps_arrival_target(tmp_path):
    original = change("08:15")
    rig = await rig_for(
        tmp_path,
        out=SETTINGS[:-1] + ", transfers: [UT]}",
        now="08:00",
        world=[original],
        extra="    transfer_min: {UT: 5}\n",
    )
    await rig.run_until("08:18")
    incoming = make_leg("AAA", "UT", "08:15", "08:28", number="20", delay_arr=11)
    short_margin = make_leg("UT", "RTD", "08:42", "08:49", number="22")
    on_time = make_leg("UT", "RTD", "08:46", "08:50", number="23")
    skip_stop = make_leg("UT", "RTD", "08:47", "08:50", number="24", cut_after="UT")
    late = make_leg("UT", "RTD", "08:48", "08:51", number="25")
    rig.provider.world = [
        make_connection([incoming, leg])
        for leg in (original.legs[1], short_margin, on_time, skip_stop, late)
    ]
    await rig.run_until("08:24")
    assert [x.train.train_number for x in rig.occ().lock.legs] == ["20", "23"]
    assert "onward train at 08:46" in told(rig, EventKind.TRANSFER_BROKEN)[0][0]
    assert rig.rt.view().occurrences[0].arrival_target_status == "on_target"


async def test_departed_onward_train_shows_the_next_connection_that_meets_target(tmp_path):
    original = change("08:15")
    rig = await rig_for(
        tmp_path,
        out=SETTINGS[:-1] + ", transfers: [UT]}",
        now="08:00",
        world=[original],
        extra="    transfer_min: {UT: 5}\n",
    )
    await rig.run_until("08:18")
    state = rig.occ()
    journey = replace(
        state.lock, legs=(replace(state.lock.legs[0], state=LegState.DONE), state.lock.legs[1])
    )
    state = replace(state, lock=journey)
    ctx = occurrence_ctx(rig.rt.state, state, at("08:39"))
    on_time = make_leg("UT", "RTD", "08:46", "08:50", number="23")
    late = make_leg("UT", "RTD", "08:47", "08:51", number="24")
    answers = (
        ans("AAA", original, when="08:39"),
        ans(
            "UT",
            make_connection([on_time]),
            make_connection([late]),
            when="08:39",
        ),
    )
    message = left_note(state, ctx, merged_pool(answers), journey, 1)
    assert "next good connection: 08:46" in message.text
    assert occurrence_view(state, ctx, answers).next_good.legs[0].train == on_time.train


async def test_missed_train_has_no_on_target_journey(tmp_path):
    rig = await rig_for(tmp_path, out=SETTINGS, now="08:00", world=[direct("08:15", "08:49")])
    await rig.run_until("08:18")
    assert rig.rt.view().occurrences[0].arrival_target_status == "on_target"
    await rig.run_until("08:27")
    await report(rig, "report", "08:27", "AAA")
    assert rig.occ().lock.legs[0].state is LegState.MISSED
    assert told(rig, EventKind.MISSED_FIRST_TRAIN)
    assert rig.rt.view().occurrences[0].arrival_target_status == "no_train"


async def test_arrival_failure_after_boarding_does_not_move_original_train(tmp_path):
    rig = await rig_for(tmp_path, out=SETTINGS, now="08:00", world=[direct("08:15", "08:49")])
    await rig.run_until("08:18")
    assert first_train(rig) == "08:15"
    rig.provider.world = [direct("08:15", "08:49", delay_arr=2), direct("08:25", "08:50", "2")]
    await rig.run_until("08:20")
    assert first_train(rig) == "08:15"
    assert rig.rt.view().occurrences[0].arrival_target_status == "late"
    assert told(rig, EventKind.ARRIVAL_TARGET) == [
        ("your train no longer meets your 09:00 arrival target.", Severity.LOUD),
    ]


async def test_arrival_off_uses_earliest_and_reloads_without_a_target_claim(tmp_path):
    trains = [direct("07:00", "08:55"), direct("08:15", "08:49", "2")]
    extra = "    alerts: {arrive_by: false}\n"
    rig = await rig_for(tmp_path, out=SETTINGS, extra=extra, world=trains)
    await rig.run_until("06:37")
    assert first_train(rig) == "07:00"
    assert rig.rt.view().occurrences[0].arrive_by is None
    assert not rig.provider.calls[0].search_for_arrival
    text = (
        PROFILE.replace("OUT", SETTINGS)
        .replace("RET", '{first_train: "17:52", buffer_min: 5}')
        .replace("LOC", "{source: ha, entity: person.me}")
    )
    rig.write(text=text)
    assert await rig.rt.reload() is None
    await rig.run_until("06:39")
    assert rig.rt.view().occurrences[0].arrival_target_status == "late"
    assert any(q.search_for_arrival for q in rig.provider.calls)
    rig.write(text=text + extra)
    assert await rig.rt.reload() is None
    await rig.run_until("06:41")
    assert rig.rt.view().occurrences[0].arrival_target_status is None
    assert not rig.provider.calls[-1].search_for_arrival


async def test_location_does_not_replace_a_valid_auto_lock_with_a_later_train(tmp_path):
    selected = direct("08:15", "08:49")
    rig = await rig_for(tmp_path, out=SETTINGS, world=[selected])
    await rig.run_until("06:37")
    rig.provider.world = [selected, direct("08:30", "08:49", "2")]
    await report(rig, "leave", "07:55")
    await report(rig, "enter", "08:01", "AAA")
    assert first_train(rig) == "08:15"
    assert rig.occ().at_station
    assert rig.occ().lock.source.value == "station"
    assert any(q.search_for_arrival and q.at == at("08:50") for q in rig.provider.calls[1:])


@pytest.mark.parametrize("action", ["yes", "other"])
async def test_other_station_rechecks_the_target_and_keeps_eligible_choices(tmp_path, action):
    early = make_connection([make_leg("UT", "RTD", "08:12", "08:35", number="1")])
    second = make_connection([make_leg("UT", "RTD", "08:22", "08:40", number="2")])
    latest = make_connection([make_leg("UT", "RTD", "08:30", "08:45", number="3")])
    rig = await rig_for(tmp_path, out=SETTINGS, now="07:50", world=[early, second, latest])
    rig.provider.positions = {"UT": (52.0, 5.0)}
    await rig.run_until("07:53")
    await report(rig, "enter", "07:53", "UT")
    ask = rig.occ().prompt
    assert ask.train.train_number == "3"
    now_late = make_connection(
        [
            make_leg("UT", "RTD", "08:30", "08:45", number="3", delay_arr=10),
        ]
    )
    rig.provider.world = [early, second, now_late]
    response = await rig.rt.answer_prompt(ask.id, action)
    assert rig.provider.calls[-1].search_for_arrival
    assert rig.occ().lock is None
    if action == "yes":
        assert response is not None and "no longer meets your arrival plan" in response.text
    else:
        assert response is None
        assert [label for _, label in rig.occ().prompt.choices] == ["08:12", "08:22"]


@pytest.mark.parametrize("answer", ["yes", "other", "legacy"])
async def test_other_station_confirms_exact_train_in_repeated_hour(tmp_path, answer):
    day = date(2026, 10, 24)
    rig = Rig(tmp_path)
    rig.clock.t = at("23:45", day)
    rig.write(
        text=PROFILE.replace(
            "OUT",
            '{mode: window, window: ["23:30", "03:10"], buffer_min: 10, '
            'pick: arrive_by, arrive_by: "03:15"}',
        )
        .replace("RET", '{first_train: "17:52", buffer_min: 5}')
        .replace("LOC", "{source: ha, entity: person.me}")
        .replace("[mon, tue, wed, thu, fri]", "[sat]")
    )
    first = make_leg("UT", "RTD", "02:20", "02:45", day=day, number="11")
    second = make_leg("UT", "RTD", "02:20", "02:45", day=day, number="11", fold_dep=1, fold_arr=1)
    rig.provider.world = [make_connection([first]), make_connection([second])]
    rig.provider.positions = {"UT": (52.0, 5.0)}
    await rig.rt.start()
    await rig.rt.poll_once()
    await rig.rt.report_location("p1", LocationEvent("UT", "enter", rig.clock.now()))
    key = "p1|outbound|2026-10-24"
    ask = rig.occ(key).prompt
    assert ask.train == second.train
    assert "02:20 CET" in told(rig, EventKind.STATION_ASK)[-1][0]
    if answer != "yes":
        assert await rig.rt.answer_prompt(ask.id, "other") is None
        ask = rig.occ(key).prompt
        if answer == "legacy":
            saved = encode(ask)
            saved.pop("trains")
            saved["choices"] = [["t0220", "02:20"]]
            previous = decode(StationPrompt, saved)
            rig.occ_set(replace(rig.occ(key), prompt=previous), key)
            rig.clock.jump(1)
            assert await rig.rt.answer_prompt(previous.id, "t0220") is None
            assert rig.occ(key).lock is None
            ask = rig.occ(key).prompt
            assert ask.id != previous.id
        (early_button, early_label), (button, late_label) = ask.choices
        assert early_button != button
        assert (early_label, late_label) == ("02:20 CEST", "02:20 CET")
        await rig.restart()
    else:
        button = "yes"

    renamed = replace(second, train=replace(second.train, train_number="13"))
    rig.provider.world = [make_connection([first]), make_connection([renamed])]
    assert await rig.rt.answer_prompt(ask.id, button) is not None
    assert rig.occ(key).lock is None and rig.occ(key).prompt.id == ask.id

    rig.provider.world = [make_connection([first]), make_connection([second])]
    assert await rig.rt.answer_prompt(ask.id, button) is None
    locked = rig.occ(key).lock.legs[0]
    assert locked.board_station == "UT"
    assert locked.train == second.train


@pytest.mark.parametrize(("target", "expected"), [("18:20", "17:30"), ("18:00", None)])
async def test_arrive_by_cannot_use_a_train_before_the_minimum_stay(tmp_path, target, expected):
    ret = STAY_RET[:-1] + f', pick: arrive_by, arrive_by: "{target}", walk_after_min: 10}}'
    rig = await rig_for(tmp_path, FIXED_OUT, ret, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=True)
    await rig.run_until("15:20")
    await report(rig, "leave", "15:20", zone="work")
    await rig.run_until("16:50")
    assert first_train(rig, RET_KEY) == expected  # the stay ends at 17:05
    assert bool(told(rig, EventKind.ARRIVAL_TARGET)) is (expected is None)


@pytest.mark.parametrize(
    ("date_", "window", "target", "walk", "expected"),
    [
        (date(2026, 10, 27), '["21:00", "23:50"]', "00:15", 30, "2026-10-27T22:45:00+00:00"),
        (date(2026, 10, 24), '["23:00", "03:00"]', "02:15", 45, "2026-10-25T00:30:00+00:00"),
        (date(2026, 3, 28), '["23:00", "03:00"]', "02:15", 15, "2026-03-29T00:45:00+00:00"),
    ],
)
def test_overnight_deadline_uses_utc_walk_arithmetic(date_, window, target, walk, expected):
    (profile,), errors = parse(
        out=(
            f"{{mode: window, window: {window}, buffer_min: 10, pick: arrive_by, "
            f'arrive_by: "{target}", walk_after_min: {walk}}}'
        )
    )
    assert not errors
    schedule = dated_schedule(profile, Direction.OUTBOUND, date_, None)
    assert schedule.arrival_deadline.isoformat() == expected


def test_query_identity_distinguishes_mode_and_repeated_clock_hour():
    dt = datetime(2026, 10, 25, 2, 30, tzinfo=TZ)
    depart = Query("AAA", "RTD", dt)
    arrive = replace(depart, search_for_arrival=True)
    assert len({depart, arrive, replace(arrive, at=dt.replace(fold=1))}) == 3
    assert arrive == replace(arrive, at=dt.astimezone(UTC))
    assert decode(Query, encode(arrive)) == arrive
    old = encode(depart)
    old.pop("search_for_arrival")
    assert decode(Query, old) == depart


async def test_held_arrival_warning_keeps_its_deadline_after_a_dst_round_trip(tmp_path):
    rig = Rig(tmp_path)
    rig.clock.t = datetime(2026, 10, 24, 23, 30, tzinfo=TZ)
    rig.write(
        text=PROFILE.replace(
            "OUT",
            '{mode: window, window: ["23:30", "03:00"], buffer_min: 10, '
            'pick: arrive_by, arrive_by: "02:15"}',
        )
        .replace("RET", '{first_train: "17:52", buffer_min: 5}')
        .replace(
            "LOC",
            "null",
        )
        .replace("[mon, tue, wed, thu, fri]", "[sat]")
    )
    rig.provider.world = []
    rig.notifier.fails = lambda e: e.kind is EventKind.ARRIVAL_TARGET
    await rig.rt.start()
    await rig.rt.poll_once()
    (warning,) = [o for o in rig.rt.state.obligations if o.event.kind is EventKind.ARRIVAL_TARGET]
    restored = decode(Obligation, encode(warning))
    assert restored.event.facts.arrival_deadline.fold == 1
    assert seam.ledger(rig.rt).fact_holds(restored)


def test_minimum_stay_and_weekday_overrides_resolve_before_ranking():
    (p,), errors = parse(
        out=SETTINGS[:-1]
        + (
            ', min_stay_min: 30, by_weekday: {tue: {arrive_by: "09:10", '
            "walk_after_min: 0, prefer_direct_min: 0}}}"
        )
    )
    assert not errors
    s = dated_schedule(p, Direction.OUTBOUND, DAY, None, at("07:05"))
    assert s.first_departure == at("07:35")
    assert s.arrive_by == at("09:10")
    assert s.arrival_deadline == at("09:10")
    assert s.prefer_direct_min == 0
    c = dated_schedule(p, Direction.OUTBOUND, DAY, None, at("08:45"))
    assert c.stay_capped
    assert decode(type(p), encode(p)).outbound == p.outbound


@pytest.mark.parametrize(
    ("scope", "extra", "field"),
    [
        (WINDOW, "", "outbound.arrive_by"),
        (WINDOW, ', arrive_by: "24:00"', "outbound.arrive_by"),
        (WINDOW, ', arrive_by: "09:00", walk_after_min: true', "outbound.walk_after_min"),
        (WINDOW, ', arrive_by: "09:00", prefer_direct_min: 0.5', "outbound.prefer_direct_min"),
        (WINDOW, ', arrive_by: "09:00", prefer_direct_min: -1', "outbound.prefer_direct_min"),
        (
            WINDOW.replace("pick: arrive_by", "pick: earliest"),
            ', arrive_by: "09:00"',
            "outbound.arrive_by",
        ),
        (
            WINDOW,
            ', by_weekday: {tue: {arrive_by: "09:00", walk_after_min: null}}',
            "outbound.by_weekday.tue.walk_after_min",
        ),
    ],
)
def test_rejects_invalid_settings(scope, extra, field):
    _, errors = parse(out=f"{{{scope}{extra}}}")
    assert any(e.path == f"profiles[0].{field}" for e in errors)

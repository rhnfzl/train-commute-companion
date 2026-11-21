from dataclasses import replace
from datetime import UTC, date, time, timedelta

import pytest
import runtime_seam as seam
from builders import make_connection, make_leg
from runtime_rig import DAY, PROFILE, Rig, at, usual_on

from train_commute_companion.daily_brief import current_text, directions_on, slots
from train_commute_companion.model import Direction, EventKind, OccurrenceKey, direction_key, okey
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import (
    ActiveDisruption,
    ActiveDisruptions,
    MalformedAnswer,
    PlannedWorks,
    ProviderError,
    RateLimited,
)
from train_commute_companion.runtime import Runtime, fresh_state

OUT, BACK = Direction.OUTBOUND, Direction.RETURN
NEXT = DAY + timedelta(days=1)
YAML = PROFILE.split('  - id: "p2"')[0].replace("NAME", "office_a")
MORNING = EventKind.DAILY_BRIEF


def world():
    days = [DAY + timedelta(days=n) for n in range(10)]
    return [usual_on(day) for day in days] + [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:40", number="600", day=day)])
        for day in days
    ]


async def setup(tmp_path, *, at_time="05:55", text=YAML):
    rig = Rig(tmp_path, at_time)
    rig.write(text=text)
    rig.provider.world = world()
    await rig.rt.start()
    rig.provider.calls.clear()
    return rig


def tells(rig, kind):
    return [event for event in rig.notifier.attempts if event.kind is kind]


async def run_at(rig, clock, day=DAY):
    rig.clock.t = at(clock, day)
    await rig.rt.scheduled_lookahead_once()


async def test_daily_checks_follow_the_recurring_schedule(tmp_path):
    rig = await setup(
        tmp_path, text=YAML + '    repeat: {start_date: "2026-10-19", every_weeks: 2}\n'
    )
    await run_at(rig, "06:00")
    await run_at(rig, "20:00")
    assert not tells(rig, MORNING)
    assert all(q.at.date() not in (DAY, NEXT) for q in rig.provider.calls)
    await run_at(rig, "06:00", DAY + timedelta(days=7))
    (brief,) = tells(rig, MORNING)
    assert brief.service_date == DAY + timedelta(days=7)


@pytest.mark.parametrize("retry, crowded", [(False, False), (True, False), (True, True)])
async def test_brief_warns_when_a_followed_train_misses_the_arrival_target(
    tmp_path, retry, crowded
):
    text = YAML.replace(
        'outbound: {first_train: "08:12", buffer_min: 10, transfers: [UT]}',
        'outbound: {mode: window, window: ["07:00", "09:00"], buffer_min: 10, '
        'pick: arrive_by, arrive_by: "09:00", walk_after_min: 10}',
    )
    text += (
        '    brief: {at: "06:37"}\n'
        if retry
        else '    brief: {at: "06:38", only_if_something: true}\n'
    )
    if crowded:
        text += "    alerts: {leave_by: false}\n"
    rig = await setup(tmp_path, at_time="06:35", text=text)
    first = make_leg("AAA", "RTD", "08:25", "08:49")
    rig.provider.world = [
        make_connection([replace(first, crowd_forecast="high") if crowded else first])
    ]
    await rig.run_until("06:37")
    if retry:
        rig.notifier.fails = lambda e: e.kind is MORNING
        await rig.rt.scheduled_lookahead_once()
        (original,) = tells(rig, MORNING)
        assert ("Leave by 08:15" in original.text) is not crowded
        assert ("crowd forecast: high" in original.text) is crowded
        rig.notifier.fails = lambda _: False
    rig.provider.world = [make_connection([make_leg("AAA", "RTD", "08:25", "08:49", delay_arr=2)])]
    rig.clock.jump(2)
    await rig.rt.poll_once()
    live = next(o for o in rig.rt.view().occurrences if o.key.direction is OUT)
    assert live.arrival_target_status == "late"
    await rig.rt.scheduled_lookahead_once()
    attempts = tells(rig, MORNING)
    assert len(attempts) == 1 + retry
    brief = attempts[-1]
    assert brief.key == attempts[0].key
    assert "No confirmed train meets your 09:00 arrival target" in brief.text
    assert "Leave by 08:15" not in brief.text


@pytest.mark.parametrize(
    "extra,field",
    [
        ("    alerts: {daily_brief: 'false'}\n", "alerts.daily_brief"),
        ("    alerts: {daily_brief: null}\n", "alerts.daily_brief"),
        ("    brief: {at: 6}\n", "brief.at"),
        ("    brief: {evening_at: '24:00'}\n", "brief.evening_at"),
        ("    brief: {only_if_something: 1}\n", "brief.only_if_something"),
    ],
)
def test_brief_settings_are_strict(extra, field):
    profiles, problems = parse_profiles(YAML + extra, None)
    assert not profiles
    assert any(problem.path.endswith(field) for problem in problems)


def test_brief_slots_respect_host_fallback_and_dst():
    p = parse_profiles(YAML, None)[0][0]
    assert p.alerts.daily_brief and not p.brief.only_if_something
    assert [x.time() for x in slots(p, DAY, time(6))] == [time(6), time(20)]
    assert slots(p, DAY, time(7, 15))[0].time() == time(7, 15)
    p = replace(p, brief=replace(p.brief, at=time(2, 30), evening_at=time(2, 45)))
    spring = slots(p, date(2027, 3, 28), time(6))
    assert [x.time() for x in spring] == [time(3), time(3)]
    fall = slots(p, date(2026, 10, 25), time(6))
    assert [x.astimezone(UTC).time() for x in fall] == [time(0, 30), time(0, 45)]


@pytest.mark.parametrize("enabled", [False, True])
async def test_mock_daily_request_budget(tmp_path, enabled):
    text = YAML if enabled else YAML + "    alerts: {daily_brief: false}\n"
    rig = await setup(tmp_path, text=text)
    await run_at(rig, "05:59")
    assert not rig.provider.calls
    await run_at(rig, "06:00")
    assert (len(rig.provider.calls), rig.provider.works_calls, rig.provider.disruption_calls) == (
        (22, 1, 1) if enabled else (20, 1, 0)
    )
    assert {q.at.date() for q in rig.provider.calls if q.at.date() > DAY} == {
        DAY + timedelta(days=n) for n in (1, 2, 3, 6, 7)
    }
    assert all(entry.last_run is not None for entry in rig.rt.state.lookahead.values())
    morning = tells(rig, MORNING)
    assert len(morning) == int(enabled)
    if morning:
        assert "Outbound: 08:12 train 1234. Leave by 08:02." in morning[0].text
        assert "Return: 17:52 train 600. Leave by 17:47." in morning[0].text
    await run_at(rig, "19:59")
    before = len(rig.provider.calls)
    await run_at(rig, "20:00")
    assert len(rig.provider.calls) - before == (4 if enabled else 0)
    assert rig.provider.works_calls == (2 if enabled else 1)
    assert not tells(rig, EventKind.WORKS_AHEAD)
    assert len(tells(rig, MORNING)) == int(enabled)


async def test_custom_slots_and_evening_does_not_seed_or_complete_horizon(tmp_path):
    text = YAML + '    brief: {at: "21:10", evening_at: "19:15"}\n'
    rig = await setup(tmp_path, text=text)
    await run_at(rig, "19:14")
    assert not rig.provider.calls
    await run_at(rig, "19:15")
    assert len(rig.provider.calls) == 4
    assert {q.at.date() for q in rig.provider.calls} == {NEXT}
    assert not rig.rt.state.baselines
    assert all(
        ledger.last_evening and ledger.last_applied is None and ledger.last_run is None
        for ledger in rig.rt.state.lookahead.values()
    )
    await rig.restart()
    rig.provider.calls.clear()
    await run_at(rig, "19:16")
    assert not rig.provider.calls
    await run_at(rig, "21:10")
    assert len(rig.provider.calls) == 20
    assert all(ledger.last_run for ledger in rig.rt.state.lookahead.values())


async def test_evening_dated_facts_survive_partial_evidence_and_restart(tmp_path):
    rig = await setup(tmp_path)
    await run_at(rig, "06:00")
    before = rig.rt.state.lookahead["p1|outbound"]
    baseline = rig.rt.state.baselines
    usual = usual_on(NEXT)
    # A dated cancelled first leg is still evidence after the second request fails.
    cancelled = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True, day=NEXT)
    rig.provider.world = [c for c in rig.provider.world if c != usual] + [
        make_connection([cancelled, usual.legs[1]])
    ]
    rig.provider.error = lambda q: (
        ProviderError("missing leg response")
        if (q.origin == "UT" and q.at.date() == NEXT)
        else None
    )
    await run_at(rig, "20:00")
    after = rig.rt.state.lookahead["p1|outbound"]
    dated = tells(rig, EventKind.WORKS_AHEAD)
    assert len(dated) == 1
    assert dated[0].service_date == NEXT
    assert "no trains" in dated[0].text
    assert (after.last_applied, after.last_run) == (before.last_applied, before.last_run)
    assert rig.rt.state.baselines == baseline

    await rig.restart()
    await run_at(rig, "20:02")
    rig.provider.error = lambda q: None
    await run_at(rig, "06:00", NEXT)
    assert len(tells(rig, EventKind.WORKS_AHEAD)) == 1
    assert len(tells(rig, MORNING)) == 2
    assert "08:12 train 1234: cancelled" in tells(rig, MORNING)[-1].text
    await rig.restart()
    await run_at(rig, "06:03", NEXT)
    assert len(tells(rig, MORNING)) == 2


async def test_brief_reports_uncertainty_and_retries_delivery_after_restart(tmp_path):
    rig = await setup(tmp_path)
    rig.provider.world = []
    rig.notifier.fails = lambda event: event.kind is MORNING

    async def works():
        return PlannedWorks((), "unknown")

    rig.provider.planned_works = works
    await run_at(rig, "06:00")
    initial = tells(rig, MORNING)
    assert len(initial) == 1
    assert "08:12 scheduled. NS train details unavailable" in initial[0].text
    assert "Planned works data unavailable" in initial[0].text
    assert rig.rt.state.brief_days["p1"] == DAY
    rig.notifier.fails = lambda event: False
    await rig.restart()
    await run_at(rig, "06:04")
    assert [event.key for event in tells(rig, MORNING)] == [initial[0].key] * 2
    assert not [o for o in rig.rt.state.obligations if o.event.kind is MORNING]


async def test_only_if_something_reuses_routed_disruption_evidence(tmp_path):
    text = YAML + "    brief: {only_if_something: true}\n"
    rig = await setup(tmp_path, text=text)
    await run_at(rig, "06:00")
    assert not tells(rig, MORNING)
    rig.provider.disruptions = ActiveDisruptions(
        (
            ActiveDisruption("relevant", "Signal failure", frozenset({"UT"}), None),
            ActiveDisruption("wrong-route", "Other route", frozenset({"OTHER"}), None),
            ActiveDisruption(
                "ends-early", "Ends early", frozenset({"UT"}), None, end=at("07:30", NEXT)
            ),
        ),
        "complete",
    )
    await run_at(rig, "06:00", NEXT)
    (brief,) = tells(rig, MORNING)
    assert "Signal failure" in brief.text
    assert "Other route" not in brief.text
    assert "Ends early" not in brief.text
    assert rig.provider.disruption_calls == 2


async def test_brief_eligibility_uses_direction_skip_pause_and_exception(tmp_path):
    text = YAML.replace("days: [mon, tue, wed, thu, fri]", "days: [wed]") + (
        f"    extra_dates: ['{DAY}']\n    pauses: [{{start: '{NEXT}', end: '{NEXT}'}}]\n"
    )
    rig = await setup(tmp_path, text=text)
    key = OccurrenceKey("p1", OUT, DAY)
    state = replace(fresh_state(key, rig.rt.config.revision, None), not_travelling=True)
    seam.seed_state(rig.rt, occurrences={okey(key): state})
    p = rig.rt.config.profiles[0]
    assert directions_on(rig.rt.state, p, DAY) == (BACK,)
    assert directions_on(rig.rt.state, p, NEXT) == ()
    await run_at(rig, "06:00")
    (brief,) = tells(rig, MORNING)
    assert "Return:" in brief.text and "Outbound:" not in brief.text
    rig.provider.calls.clear()
    await run_at(rig, "20:00")
    assert not rig.provider.calls


@pytest.mark.parametrize("phase", ["morning", "evening"])
async def test_due_requests_discard_old_settings(tmp_path, phase):
    text = YAML + '    brief: {at: "09:30", evening_at: "05:20"}\n'
    rig = await setup(tmp_path, text=text)
    slot = "09:30" if phase == "morning" else "05:20"
    if phase == "morning":
        # A successful horizon already exists at this slot. The request belongs to the brief.
        rig.clock.set(slot)
        await rig.rt.lookahead_once()
        rig.provider.calls.clear()

    async def edit(query):
        rig.provider.hook = None
        rig.write(text=text + "    alerts: {daily_brief: false}\n")
        await rig.rt.reload()

    rig.provider.hook = edit
    await run_at(rig, slot)
    assert len(rig.provider.calls) == 1
    assert not tells(rig, MORNING)
    assert not tells(rig, EventKind.WORKS_AHEAD)
    assert not rig.rt.state.brief_days
    assert all(ledger.last_evening is None for ledger in rig.rt.state.lookahead.values())


async def test_coincident_checks_reuse_tomorrows_trip_responses_and_dedup(tmp_path):
    text = YAML + '    brief: {evening_at: "06:00"}\n'
    rig = await setup(tmp_path, text=text)
    usual = usual_on(NEXT)
    cancelled = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True, day=NEXT)
    rig.provider.world = [c for c in rig.provider.world if c != usual] + [
        make_connection([cancelled, usual.legs[1]])
    ]
    await run_at(rig, "06:00")
    # The full check learns a different reference time for the second leg.
    assert len(set(rig.provider.calls)) == len(rig.provider.calls) == 23
    assert rig.provider.works_calls == 1
    assert [e.service_date for e in tells(rig, EventKind.WORKS_AHEAD)] == [NEXT]
    await rig.restart()
    rig.provider.calls.clear()
    await run_at(rig, "06:05")
    assert not rig.provider.calls
    assert len(tells(rig, EventKind.WORKS_AHEAD)) == 1


async def test_brief_uses_mocked_weather_and_crowd_and_routes_to_aux(tmp_path):
    from test_disruption_receipts import EMAIL, OTHER, World

    from train_commute_companion.providers.weather import WeatherHour, WeatherView

    world_ = World(tmp_path)
    world_.port.channels[OTHER] = "webhook"
    rig = world_.rig
    rig.clock.set("05:55")
    rig.write(text=YAML + "    alerts: {channels: {routine: [email]}}\n")
    usual = usual_on(DAY)
    crowded = make_connection([replace(usual.legs[0], crowd_forecast="high"), usual.legs[1]])
    rig.provider.world = [c for c in world() if c != usual] + [crowded]
    rig.provider.positions = {"AAA": (52.1, 5.1), "RTD": (51.9, 4.5)}

    class Weather:
        def __init__(self):
            self.stations = {}

        async def refresh(self, stations):
            self.stations.update(stations)

        def snapshot(self, station, position, when, now):
            assert position == self.stations[station]
            rain = WeatherHour(when, 8, 2, 0, 61)
            return WeatherView(station, when, rain, now, (), None, None)

    weather = Weather()
    rig.rt._weather = weather
    await world_.start()
    await rig.rt.weather_idle()
    await run_at(rig, "06:00")
    await rig.rt.webhooks_idle()
    (brief,) = [e for e in world_.records[EMAIL] if e.kind is MORNING]
    assert "AAA-UT high" in brief.text
    assert "Leave by 08:00 (+2 min suggested for forecast rain)" in brief.text
    assert "AAA forecast 8°C, 2 mm precipitation" in brief.text
    assert set(weather.stations) == {"AAA", "RTD"}
    assert not tells(rig, MORNING)
    assert not [e for e in world_.records[OTHER] if e.kind is MORNING]


async def test_evening_warns_but_only_full_horizon_adopts_a_clean_move(tmp_path):
    from train_commute_companion.lookahead_probe import signature_of
    from train_commute_companion.state import BaselineHistory, BaselineVersion

    text = YAML + ('    brief: {at: "21:10"}\n    alerts: {timetable_horizon_count: 1}\n')
    rig = await setup(tmp_path, text=text)
    old = BaselineHistory((BaselineVersion(1, signature_of(usual_on(DAY), DAY), DAY, True),))
    seam.seed_state(rig.rt, baselines={"p1|outbound": old})
    rig.provider.world = [c for c in rig.provider.world if c != usual_on(NEXT)] + [
        usual_on(NEXT, "08:19")
    ]
    await run_at(rig, "20:00")
    assert rig.rt.state.baselines["p1|outbound"] == old
    assert rig.rt.state.lookahead["p1|outbound"].last_run is None
    assert [e.service_date for e in tells(rig, EventKind.WORKS_AHEAD)] == [NEXT]
    assert not tells(rig, EventKind.TIMETABLE_ADOPTED)
    await run_at(rig, "21:10")
    assert rig.rt.state.baselines["p1|outbound"].latest == 2
    assert [e.service_date for e in tells(rig, EventKind.TIMETABLE_ADOPTED)] == [NEXT]


async def test_overnight_return_is_labelled_with_its_service_date(tmp_path):
    text = YAML.replace('first_train: "17:52",', 'first_train: "00:30", day_offset: 1,')
    rig = await setup(tmp_path, text=text)
    rig.provider.world.append(
        make_connection([make_leg("RTD", "AAA", "00:30", "01:10", number="601", day=DAY)])
    )
    await run_at(rig, "06:00")
    (brief,) = tells(rig, MORNING)
    assert "Return (Wed 28 Oct): 00:30 train 601. Leave by 00:25." in brief.text


async def test_normal_evening_start_does_not_send_an_expired_morning_brief(tmp_path):
    rig = await setup(tmp_path, at_time="20:00")
    await rig.rt.scheduled_lookahead_once()
    assert not tells(rig, MORNING)
    assert not tells(rig, EventKind.WORKS_AHEAD)
    assert rig.provider.disruption_calls == 0
    assert all(ledger.last_evening is not None for ledger in rig.rt.state.lookahead.values())


def test_brief_rejects_duplicate_yaml_keys_through_aliases():
    text = 'defaults: &brief {at: "08:30", at: "06:30"}\n' + YAML + "    brief: *brief\n"
    profiles, problems = parse_profiles(text, None)
    assert not profiles
    assert any("duplicate" in problem.message and "at" in problem.message for problem in problems)


@pytest.mark.parametrize(
    "extra,field",
    [
        ('    brief: {at: "08:30", at: "06:30"}\n', "at"),
        ('    brief: {at: "08:30", !!value at: "06:30"}\n', "at"),
        ('    brief: {at: "08:30", !!timestamp at: "06:30"}\n', "at"),
        ("    brief: {}\n    brief: {}\n", "brief"),
        ("    alerts: {daily_brief: false, daily_brief: true}\n", "daily_brief"),
        ("    alerts: {daily_brief: true, !!timestamp daily_brief: false}\n", "daily_brief"),
    ],
)
def test_brief_rejects_duplicate_yaml_keys(extra, field):
    profiles, problems = parse_profiles(YAML + extra, None)
    assert not profiles
    assert any("duplicate" in problem.message and field in problem.message for problem in problems)


def test_brief_supports_explicit_override_of_merged_default():
    text = 'defaults: &brief {at: "08:30"}\n' + YAML + '    brief: {<<: *brief, at: "06:30"}\n'
    profiles, problems = parse_profiles(text, None)
    assert not problems
    assert profiles[0].brief.at == time(6, 30)


def test_brief_preserves_profile_anchor():
    text = YAML.replace("profiles:\n  - ", "defaults:\n  - &office\n    ")
    text += '    brief: {at: "06:30"}\nprofiles: [*office]\n'
    profiles, problems = parse_profiles(text, None)
    assert not problems
    assert profiles[0].brief.at == time(6, 30)


async def test_conflicting_boarding_evidence_keeps_evening_warning_open(tmp_path):
    rig = await setup(tmp_path)
    usual = usual_on(NEXT)
    first, second = usual.legs
    conflict = replace(first, stops=(replace(first.board, cancelled=True), *first.stops[1:]))
    rig.provider.world.remove(usual)
    cancelled = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True, day=NEXT)
    rig.provider.world.append(make_connection([cancelled, second]))
    await run_at(rig, "06:00")
    assert tells(rig, EventKind.WORKS_AHEAD)
    rig.provider.world.pop()
    rig.provider.world.extend([usual, make_connection([conflict, second])])
    await run_at(rig, "20:00")
    assert not tells(rig, EventKind.RESTORED)
    assert rig.rt.state.lookahead[direction_key("p1", OUT)].evening_failures == 1
    before = len(rig.provider.calls)
    await run_at(rig, "20:15")
    assert len(rig.provider.calls) > before
    assert not tells(rig, EventKind.RESTORED)


@pytest.mark.parametrize("issue", ["cancelled", "cut short", "connection at UT is broken"])
async def test_live_brief_warns_when_connecting_journey_is_unusable(tmp_path, issue):
    text = YAML + "    brief: {only_if_something: true}\n"
    rig = await setup(tmp_path, at_time="07:50", text=text)
    usual = usual_on(DAY)
    first, second = usual.legs
    if issue == "cancelled":
        second = make_leg("UT", "RTD", "08:50", "09:24", number="5678", cancelled=True)
    elif issue == "cut short":
        second = make_leg(
            "UT", "RTD", "08:50", "09:24", number="5678", stops=("MID",), cut_after="MID"
        )
    else:
        first = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=14)
    rig.provider.world = [make_connection([first, second])]
    await rig.rt.poll_once()
    observed = rig.rt.view().occurrences[0]
    assert observed.health.fresh
    if issue.startswith("connection"):
        assert observed.transfers[0].state.value == "broken"
    else:
        assert observed.legs[1].state.value == issue.replace(" ", "_")
    rig.provider.world.extend(c for c in world() if c != usual)
    await run_at(rig, "07:52")
    (brief,) = tells(rig, MORNING)
    outbound = next(line for line in brief.text.splitlines() if line.startswith("Outbound"))
    assert issue in outbound
    assert "Leave by" not in outbound


async def test_brief_ignores_live_warning_for_the_previous_route_after_edit(tmp_path):
    rig = await setup(tmp_path, at_time="07:50")
    first, _ = usual_on(DAY).legs
    cancelled = make_leg("UT", "RTD", "08:50", "09:24", number="5678", cancelled=True)
    rig.provider.world = [make_connection([first, cancelled])]
    await rig.rt.poll_once()
    assert rig.rt.view().occurrences[0].legs[1].state.value == "cancelled"

    rig.write(
        text=YAML.replace("RTD", "BBB")
        .replace('first_train: "08:12"', 'first_train: "08:18"')
        .replace(", transfers: [UT]", "")
    )
    await rig.rt.reload()
    rig.provider.world = [make_connection([make_leg("AAA", "BBB", "08:18", "09:20")])]
    await run_at(rig, "07:52")

    (brief,) = tells(rig, MORNING)
    outbound = next(line for line in brief.text.splitlines() if line.startswith("Outbound"))
    assert "08:18 train" in outbound
    assert "cancelled" not in outbound


@pytest.mark.parametrize("retry_at", ["08:05", "12:00", "23:00"])
async def test_brief_retry_rechecks_advice_and_windows_for_all_destinations(tmp_path, retry_at):
    from test_disruption_receipts import EMAIL, OTHER, World

    world_ = World(tmp_path)
    rig = world_.rig
    rig.clock.set("05:55")
    rig.write(text=YAML + "    alerts: {channels: {routine: [primary, email]}}\n")
    rig.provider.world = world()
    world_.port.down = dict.fromkeys((EMAIL, OTHER), at(retry_at))
    rig.notifier.fails = lambda event: event.kind is MORNING
    await world_.start()
    await run_at(rig, "06:00")
    await rig.rt.webhooks_idle()
    (first,) = tells(rig, MORNING)
    assert "Leave by 08:02" in first.text
    rig.notifier.fails = lambda event: False
    rig.clock.set(retry_at)
    world_.build()
    await world_.start()
    await rig.rt.watchdog_once()
    await rig.rt.webhooks_idle()

    primary = tells(rig, MORNING)[1:]
    email = [e for e in world_.records[EMAIL] if e.kind is MORNING]
    assert [e.text for e in primary] == [e.text for e in email]
    if retry_at == "23:00":
        assert not primary
        assert not [o for o in rig.rt.state.obligations if o.event.kind is MORNING]
        return
    (brief,) = primary
    assert "Leave by 08:02" not in brief.text
    assert ("Outbound:" in brief.text) == (retry_at == "08:05")
    assert "Return: 17:52 train 600. Leave by 17:47." in brief.text


@pytest.mark.parametrize("setting", ["live_today", "telegram_status_pin"])
async def test_display_toggle_preserves_a_brief_waiting_to_send(tmp_path, setting):
    rig = await setup(tmp_path)
    rig.notifier.fails = lambda event: event.kind is MORNING
    await run_at(rig, "06:00")
    (original,) = tells(rig, MORNING)

    rig.write(text=YAML + f"    alerts: {{{setting}: false}}\n")
    rig.notifier.fails = lambda _: False
    rig.clock.set("06:05")
    await rig.rt.reload()
    await rig.rt.watchdog_once()

    before, sent = tells(rig, MORNING)
    assert before == original
    assert sent.text == original.text
    assert not [o for o in rig.rt.state.obligations if o.event.kind is MORNING]


@pytest.mark.parametrize("kind", [MORNING, EventKind.WORKS_AHEAD])
async def test_scheduled_alert_keeps_only_eligible_original_recipients(tmp_path, kind, monkeypatch):
    from collections import defaultdict

    from test_disruption_receipts import EMAIL, OTHER, Aux

    morning = kind is MORNING
    rig = Rig(tmp_path, "05:55" if morning else "19:55")
    profile = YAML if morning else YAML + '    brief: {at: "21:00"}\n'
    rig.write(text=profile)
    rig.provider.world = world()
    if not morning:
        (first, second) = usual_on(NEXT).legs
        cancelled = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True, day=NEXT)
        rig.provider.world.remove(make_connection([first, second]))
        rig.provider.world.append(make_connection([cancelled, second]))
    records = defaultdict(list)
    port = Aux(rig.clock.now, records)
    port.channels[OTHER] = "webhook"
    original = frozenset(port.destination_ids.values())
    retry = "06:10" if morning else "20:10"
    port.down = dict.fromkeys(port.urls, at(retry))
    phone = "ntfy:original"
    saved = rig.store.save
    written = []

    def record(state):
        saved(state)
        written.extend(o for o in state.obligations if o.event.kind is kind)

    monkeypatch.setattr(rig.store, "save", record)
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        webhooks=port,
        primary_destination=lambda: phone,
        primary_destination_scope=lambda destination: destination.startswith("ntfy:"),
    )
    rig.notifier.fails = lambda event: event.kind is kind
    await rig.rt.start()
    await run_at(rig, "06:00" if morning else "20:00")
    await rig.rt.webhooks_idle()
    (alert,) = [o for o in rig.store.load().obligations if o.event.kind is kind]
    assert written[0].target_ids == alert.target_ids == original | {phone}

    port.destination_ids[OTHER] = "webhook:replacement"
    rig.write(text=profile + "    alerts: {ntfy_enabled: false}\n")
    rig.notifier.fails = lambda event: False
    rig.clock.set(retry)
    await rig.rt.reload()
    await rig.rt.webhooks_idle()

    assert len(tells(rig, kind)) == 1
    assert [e.key for e in records[EMAIL] if e.kind is kind] == [alert.event.key]
    assert not [e for e in records[OTHER] if e.kind is kind]


@pytest.mark.parametrize("finished", ["12:00", "23:00", "00:10"])
async def test_slow_morning_batch_rechecks_travel_windows(tmp_path, finished):
    rig = await setup(tmp_path)
    await rig.rt.lookahead_once()

    def delay(query):
        if query.origin == "RTD" and query.at.date() == DAY:
            rig.clock.t = at(finished, NEXT if finished == "00:10" else DAY)

    rig.provider.hook = delay
    await run_at(rig, "06:00")
    briefs = tells(rig, MORNING)
    if finished == "12:00":
        (brief,) = briefs
        assert "Outbound:" not in brief.text
        assert "Return:" in brief.text
    else:
        assert not briefs


@pytest.mark.parametrize("enabled,severity", [(True, "loud"), (False, "silent")])
async def test_overlapping_checks_warn_once_for_tomorrows_overnight_return(
    tmp_path, enabled, severity
):
    text = YAML.replace('first_train: "17:52",', 'first_train: "00:30", day_offset: 1,')
    if not enabled:
        text += "    alerts: {daily_brief: false}\n"
    rig = await setup(tmp_path, text=text + '    brief: {evening_at: "06:00"}\n')
    rig.provider.world = [usual_on(DAY + timedelta(days=n)) for n in range(10)] + [
        make_connection(
            [
                make_leg(
                    "RTD",
                    "AAA",
                    "00:30",
                    "01:10",
                    number="601",
                    day=DAY + timedelta(days=n),
                    cancelled=n == 1,
                )
            ]
        )
        for n in range(10)
    ]
    await run_at(rig, "06:00")
    (warning,) = tells(rig, EventKind.WORKS_AHEAD)
    assert warning.service_date == NEXT + timedelta(days=1)
    assert warning.severity.value == severity


async def test_legacy_brief_with_unknown_actionability_is_dropped(tmp_path):
    rig = await setup(tmp_path, text=YAML + "    brief: {only_if_something: true}\n")
    usual = usual_on(DAY)
    rig.provider.world.remove(usual)
    rig.provider.world.append(
        make_connection(
            [
                make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True),
                usual.legs[1],
            ]
        )
    )
    rig.notifier.fails = lambda event: event.kind is MORNING
    await run_at(rig, "06:00")
    (queued,) = [o for o in rig.rt.state.obligations if o.event.kind is MORNING]
    legacy = replace(queued, brief_rows=(), brief_directions=(OUT, BACK))
    assert current_text(rig.rt.state, legacy, at("12:00")) is None


async def test_brief_states_warning_even_when_earlier_delivery_failed(tmp_path):
    text = YAML + "    brief: {only_if_something: true}\n"
    rig = await setup(tmp_path, text=text)
    rig.notifier.fails = lambda event: event.kind is EventKind.WORKS_AHEAD
    usual = usual_on(NEXT)
    rig.provider.world.remove(usual)
    rig.provider.world.append(
        make_connection(
            [
                make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True, day=NEXT),
                usual.legs[1],
            ]
        )
    )
    await run_at(rig, "20:00")
    assert tells(rig, EventKind.WORKS_AHEAD)
    await run_at(rig, "06:00", NEXT)
    (brief,) = tells(rig, MORNING)
    assert "earlier timetable alert" not in brief.text
    assert "cancelled" in brief.text


async def test_edit_rechecks_only_affected_evening_itinerary_after_restart(tmp_path):
    text = YAML + '    brief: {at: "21:10"}\n'
    rig = await setup(tmp_path, text=text)
    await run_at(rig, "20:00")
    rig.provider.calls.clear()
    rig.write(text=text.replace("office_a", "Renamed office"))
    await rig.rt.reload()
    await run_at(rig, "20:02")
    assert not rig.provider.calls
    rig.write(text=text.replace('first_train: "08:12"', 'first_train: "08:34"'))
    await rig.rt.reload()
    await rig.restart()
    rig.provider.calls.clear()
    await run_at(rig, "20:04")
    assert rig.provider.calls
    assert all(q.at.date() == NEXT and q.origin != "RTD" for q in rig.provider.calls)


async def test_malformed_evening_responses_back_off_across_restart(tmp_path):
    rig = await setup(tmp_path)
    await run_at(rig, "06:00")
    rig.provider.calls.clear()
    rig.provider.works_calls = 0
    rig.provider.error = lambda q: MalformedAnswer("bad response")
    await run_at(rig, "20:00")
    assert len(rig.provider.calls) == 2
    await rig.restart()
    for clock in ("20:02", "20:04", "20:06"):
        await run_at(rig, clock)
    assert len(rig.provider.calls) == 2
    assert rig.provider.works_calls == 1
    rig.provider.error = lambda q: None
    await run_at(rig, "20:15")
    assert len(rig.provider.calls) == 6
    assert all(entry.last_evening is not None for entry in rig.rt.state.lookahead.values())


async def test_first_evening_warns_about_meaningful_shift_without_adopting(tmp_path):
    text = YAML + '    brief: {at: "21:10"}\n'
    rig = await setup(tmp_path, text=text)
    rig.provider.world.remove(usual_on(NEXT))
    rig.provider.world.append(usual_on(NEXT, "08:19"))
    await run_at(rig, "20:00")
    (warning,) = tells(rig, EventKind.WORKS_AHEAD)
    assert warning.service_date == NEXT
    assert "08:12" in warning.text and "08:19" in warning.text
    assert not rig.rt.state.baselines
    assert not tells(rig, EventKind.TIMETABLE_ADOPTED)
    await run_at(rig, "21:10")
    assert not tells(rig, EventKind.RESTORED)


async def test_evening_retries_when_no_dated_trips_are_observed(tmp_path):
    rig = await setup(tmp_path)
    await run_at(rig, "06:00")
    rig.provider.world.remove(usual_on(NEXT))
    key = direction_key("p1", OUT)
    await run_at(rig, "20:00")
    ledger = rig.rt.state.lookahead[key]
    assert ledger.last_evening is None
    assert ledger.evening_failures == 1
    rig.provider.world.append(usual_on(NEXT, "08:19"))
    await run_at(rig, "20:15")
    ledger = rig.rt.state.lookahead[key]
    assert ledger.last_evening == at("20:15")
    assert ledger.evening_failures == 0
    (warning,) = tells(rig, EventKind.WORKS_AHEAD)
    assert warning.service_date == NEXT and "08:19" in warning.text


async def test_evening_exhausts_retries_without_marking_a_failed_check_complete(tmp_path):
    rig = await setup(tmp_path)
    await run_at(rig, "06:00")
    rig.provider.calls.clear()
    rig.provider.error = lambda q: MalformedAnswer("bad response")
    for clock in ("20:00", "20:15", "20:45", "21:30", "23:59"):
        await run_at(rig, clock)
    assert len(rig.provider.calls) == 6
    assert all(entry.last_evening is None for entry in rig.rt.state.lookahead.values())


async def test_evening_uses_the_shared_ns_cooldown(tmp_path):
    rig = await setup(tmp_path)
    await run_at(rig, "06:00")
    rig.provider.calls.clear()
    rig.provider.works_calls = 0
    rig.provider.error = lambda q: RateLimited()
    await run_at(rig, "20:00")
    assert len(rig.provider.calls) == 1
    await run_at(rig, "20:02")
    assert len(rig.provider.calls) == rig.provider.works_calls == 1
    rig.provider.error = lambda q: None
    await run_at(rig, "20:05")
    assert [q.origin for q in rig.provider.calls] == ["AAA", "RTD"]

"""Runtime safety rules end to end: news episodes, escalation, rule 5 delivery, location and
look-ahead writers, the shared budget, the poll deadline, baselines and health (7.1, 7.11,
7.12 rules 4, 5, 10, 11, 12, 13, 14, 21)."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, time, timedelta

import pytest
from builders import make_connection, make_leg
from test_lifecycle_follow import FROM_UT, L1, L1X, L2, L2X, LATER, R2, RIDE, T_FULL, journey
from test_runtime import (
    DAY,
    PROFILE,
    USUAL,
    FakeClock,
    Rig,
    at,
    every_day,
    kinds,
    started,
)

from train_commute_companion.delivery import Obligation
from train_commute_companion.lifecycle import DirectionState, NotTravelling
from train_commute_companion.location import LocationEvent
from train_commute_companion.lookahead import BaselineHistory, BaselineVersion
from train_commute_companion.model import (
    AlertEvent,
    EventKind,
    Provenance,
    Severity,
    Signature,
    SignatureLeg,
)
from train_commute_companion.providers.base import (
    AuthError,
    MalformedAnswer,
    Query,
    RateLimited,
    ServerError,
)
from train_commute_companion.store import PersistedState


def at_risk_l1() -> object:
    # arrives UT 08:45: 5 min to the 08:50, minimum 4 + 2: at risk, not broken
    return make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=5, delay_arr=5)


def late_l2(minutes: int) -> object:
    return make_leg("UT", "RTD", "08:50", "09:24", number="5678", delay_dep=minutes,
                    delay_arr=minutes)  # fmt: skip


# --- finding 2: a fact that ended and comes back is news again ---


async def test_cancel_restore_cancel_alerts_twice(tmp_path):
    rig = await started(tmp_path)
    for hhmm, world in (("07:52", [make_connection([L1X, L2]), LATER]),
                        ("07:54", [USUAL, LATER]),
                        ("07:56", [make_connection([L1X, L2]), LATER])):  # fmt: skip
        rig.clock.set(hhmm)
        rig.provider.world = world
        await rig.rt.poll_once()
    got = kinds(rig.rt.events_log)
    assert got.count(EventKind.CANCELLED) == 2 and EventKind.RESTORED in got


async def test_delay_seven_ten_seven_is_three_messages(tmp_path):
    rig = await started(tmp_path)
    for hhmm, late in (("07:52", 7), ("07:54", 10), ("07:56", 7)):
        rig.clock.set(hhmm)
        rig.provider.world = [make_connection([L1, late_l2(late)])]
        await rig.rt.poll_once()
    assert kinds(rig.rt.events_log).count(EventKind.DELAYED) == 3


# --- finding 3: crossing the arrival limit with a known delay goes loud ---


async def test_crossing_latest_arrival_by_two_minutes_is_sent_loud(tmp_path):
    rig = Rig(tmp_path)
    limit = '    alerts: {latest_acceptable_arrival: "09:30"}\n    return: {first_train: "17:52"'
    rig.write(text=PROFILE.replace("NAME", "office_a").replace(
        '    return: {first_train: "17:52"', limit, 1))  # fmt: skip
    await rig.rt.start()
    for hhmm, late in (("07:52", 5), ("07:54", 7)):  # arrives 09:29, then 09:31
        rig.clock.set(hhmm)
        rig.provider.world = [make_connection([L1, late_l2(late)])]
        await rig.rt.poll_once()
    delayed = [e for e in rig.rt.events_log if e.kind is EventKind.DELAYED]
    assert [e.severity for e in delayed] == [Severity.SILENT, Severity.LOUD]


# --- finding 4: a settings edit keeps pending messages ---


async def test_rename_keeps_a_pending_at_risk_warning(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.TRANSFER_AT_RISK
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([at_risk_l1(), L2])]
    await rig.rt.poll_once()
    assert any(o.event.kind is EventKind.TRANSFER_AT_RISK for o in rig.rt.state.obligations)
    rig.write("office_x")
    await rig.rt.reload()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    told = [e for e in rig.rt.events_log if e.kind is EventKind.TRANSFER_AT_RISK]
    assert len(told) == 1 and told[0].text.startswith("Office X:")


# --- finding 5: delivery regenerates the fact, reconciles first, reads the clock each time ---


async def test_pending_at_risk_dropped_once_the_transfer_recovered(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.TRANSFER_AT_RISK
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([at_risk_l1(), L2])]
    await rig.rt.poll_once()
    rig.clock.set("07:54")
    rig.provider.world = [USUAL]  # the delay is gone: the change is fine again
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    assert EventKind.TRANSFER_AT_RISK not in kinds(rig.rt.events_log)
    assert not any(o.event.kind is EventKind.TRANSFER_AT_RISK for o in rig.rt.state.obligations)


async def test_restart_reconciles_before_sending_stored_messages(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.TRANSFER_AT_RISK
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([at_risk_l1(), L2])]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    rig.provider.world = [USUAL]  # recovered while the app was down
    rig.clock.set("07:58")
    rt = await rig.restart()
    assert EventKind.TRANSFER_AT_RISK not in kinds(rt.events_log)


async def test_each_message_is_judged_on_the_clock_at_its_own_send(tmp_path):
    rig = await started(tmp_path)
    now = rig.clock.now()

    def app(kind: EventKind, tag: str, expires=None) -> Obligation:
        e = AlertEvent("", "app", None, None, None, kind, Severity.LOUD, None, f"App: {tag}",
                       tag, now)  # fmt: skip
        return Obligation(tag, e, None, None, 1, None, tag, expires, 0)

    slow = app(EventKind.STORAGE_FAILED, "slow")
    advice = app(EventKind.LEAVE_NOW, "advice", now + timedelta(minutes=10))
    rig.rt._st = replace(rig.rt._st, obligations=(slow, advice))
    plain = rig.notifier.send

    async def send(e: AlertEvent) -> bool:
        if e.key == "slow":
            rig.clock.jump(20)  # a 20-min send: the advice expired meanwhile
        return await plain(e)

    rig.notifier.send = send  # type: ignore[method-assign]
    await rig.rt.watchdog_once()
    assert [e.key for e in rig.rt.events_log if e.key in ("slow", "advice")] == ["slow"]


# --- finding 6: a location batch never writes back an old copy ---


async def test_location_does_not_resurrect_a_skipped_occurrence(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    mon = date(2026, 10, 26)
    night = make_leg("AAA", "RTD", "23:50", "03:40", number="9999", day=mon)
    from test_lifecycle_follow import journey, occ

    from train_commute_companion.lifecycle import LegState

    ride = replace(journey([night], (LegState.RIDING,)), planned_final_arrival=at("08:40"))
    cfg = rig.rt.config
    old = replace(occ(ride), key=replace(occ(None).key, service_date=mon),
                  route_revision=cfg.revision, settings_revision=cfg.revision)  # fmt: skip
    rig.occ_set(old, "p1|outbound|2026-10-26")
    calls = []

    async def skip_first_during_second(q: Query) -> None:
        calls.append(q)
        if len(calls) == 2:
            await rig.rt.act("p1", NotTravelling(mon, False))

    rig.provider.hook = skip_first_during_second
    await rig.rt.location("p1", LocationEvent("home", "leave", rig.clock.now(), rig.clock.now()))
    assert len(calls) == 2
    s = rig.occ("p1|outbound|2026-10-26")
    assert s is None or s.not_travelling


# --- finding 7: look-ahead never stamps a newer revision on an old profile ---


def returns_every_day(first: str) -> list:
    n = f"7{first[3:]}"
    days = [DAY + timedelta(days=i) for i in range(12)]
    return [
        make_connection([make_leg("RTD", "AAA", first, "18:30", number=n, day=d)]) for d in days
    ]


async def test_lookahead_sibling_direction_uses_the_profile_of_its_revision(tmp_path):
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = [*every_day(), *returns_every_day("17:52"), *returns_every_day("17:40")]
    await rig.rt.start()
    edited = PROFILE.replace("NAME", "office_a").replace('"17:52"', '"17:40"')

    async def edit_during_outbound(q: Query) -> None:
        rig.provider.hook = None
        rig.write(text=edited)
        await rig.rt.reload()

    rig.provider.hook = edit_during_outbound
    await rig.rt.lookahead_once()
    hist = rig.rt.state.baselines.get("p1|return", BaselineHistory())
    assert hist.versions, "the return direction runs under the new revision"
    assert all(v.signature.legs[0].departure == time(17, 40) for v in hist.versions)


# --- finding 8: first-day advice has the usual's own arrival ---


async def test_first_day_advice_before_auto_lock_on_a_cancelled_usual(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952", cancelled=True)]),
        make_connection([make_leg("RTD", "AAA", "17:22", "18:00", number="7922")]),
        make_connection([make_leg("RTD", "AAA", "18:22", "19:00", number="7982")]),
    ]
    await rig.rt.start()
    s = rig.occ("p1|return|2026-10-27")
    assert s is not None and s.lock is None  # before the 17:32 auto lock
    assert EventKind.LEAVE_NOW in kinds(rig.rt.events_log)
    assert s.advice is not None and s.advice.usual_arrival == at("18:30")


# --- finding 9: before a lock the usual train is judged on what the answer showed ---


async def test_planned_only_usual_before_lock_is_cannot_see_train(tmp_path):
    rig = Rig(tmp_path, "07:43")
    rig.provider.world = [make_connection([make_leg("AAA", "UT", "08:12", "08:40",
                          number="1234", provenance=Provenance.PLANNED), L2])]  # fmt: skip
    await rig.rt.start()
    assert rig.occ().lock is None
    await rig.rt.watchdog_once()
    blind = [e for e in rig.rt.events_log if e.kind is EventKind.CANNOT_SEE_TRAIN]
    assert len(blind) == 1 and "no reported departure time" in blind[0].text


# --- finding 10: one deadline per poll, fixed start times, watchdog during start ---


async def test_poll_deadline_covers_the_whole_poll(tmp_path):
    rig = await started(tmp_path)
    rig.clock.set("08:00")
    rig.provider.hook = lambda q: rig.clock.jump(80 / 60)  # each request 80 s
    await rig.rt.poll_once()
    assert len(rig.provider.calls) > 2
    assert rig.occ().poll_records[-1].error == "timeout"


class Stop(Exception):
    pass


async def test_loops_keep_fixed_start_times():
    from train_commute_companion.runtime import every

    clock = FakeClock(at("08:00"))
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            raise Stop
        clock.jump(seconds / 60)

    clock.sleep = sleep  # type: ignore[method-assign]

    async def slow_step() -> None:
        clock.jump(50 / 60)

    with pytest.raises(Stop):
        await every(clock, 120, slow_step)
    assert slept == [70, 70]


async def test_watchdog_runs_while_start_reconciles(tmp_path):
    rig = Rig(tmp_path)
    ran: list[str] = []
    seen_during_start: list[int] = []
    rt = rig.rt
    plain = rt.watchdog_once

    async def watchdog() -> None:
        ran.append("w")
        await plain()

    rt.watchdog_once = watchdog  # type: ignore[method-assign]

    starting = []
    plain_start = rt.start

    async def start() -> None:
        starting.append(True)
        await plain_start()
        starting.clear()

    rt.start = start  # type: ignore[method-assign]

    async def slow_reconcile(q: Query) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        if starting:
            seen_during_start.append(len(ran))

    rig.provider.hook = slow_reconcile
    sleeps = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)
        if seen_during_start or len(sleeps) > 50:
            raise Stop

    rig.clock.sleep = sleep  # type: ignore[method-assign]
    with pytest.raises(Stop):
        await rt.run_forever()
    assert seen_during_start and seen_during_start[0] >= 1


# --- finding 11: one shared budget, a late success never clears a newer cooldown ---


async def test_older_success_keeps_a_newer_cooldown(tmp_path):
    rig = await started(tmp_path)
    ev = asyncio.Event()

    async def hook(q: Query) -> None:
        if q.origin == "AAA":
            await ev.wait()

    def error(q: Query) -> Exception | None:
        if q.origin == "UT":
            ev.set()
            return RateLimited()
        return None

    rig.provider.hook = hook
    rig.provider.error = error
    now = rig.clock.now()
    a, b = await asyncio.gather(
        rig.rt._fetch(Query("AAA", "RTD", now), False, None),
        rig.rt._fetch(Query("UT", "RTD", now), True, None),
    )
    assert not isinstance(a, str) and b == "rate"
    assert rig.rt._budget.cooldown_until is not None


async def test_station_lookup_waits_on_the_shared_cooldown(tmp_path):
    rig = await started(tmp_path)
    rig.provider.error = lambda q: RateLimited()
    rig.clock.set("07:52")
    await rig.rt.poll_once()
    n = rig.rt.requests
    assert await rig.rt.stations() is None and rig.rt.requests == n


# --- finding 12: baselines stay with the occurrences that use them ---


def sig(first: time, second: time) -> Signature:
    return Signature((SignatureLeg("AAA", "UT", first, 0, 20), SignatureLeg("UT", "RTD", second,
                      0, 34)))  # fmt: skip


async def test_route_reseed_keeps_a_locked_occurrences_adopted_schedule(tmp_path):
    rig = Rig(tmp_path, "07:56")
    hist = BaselineHistory((BaselineVersion(1, sig(time(8, 20), time(8, 50)), date(2026, 10, 1),
                            True),))  # fmt: skip
    rig.store.save(PersistedState(None, {}, {"p1|outbound": hist}, {}, {}, (), True))
    rig.provider.world = every_day("08:20")
    await rig.rt.start()
    s = rig.occ()
    assert s.lock is not None and s.lock.legs[0].train.planned_departure == at("08:20")
    rig.write(text=PROFILE.replace("NAME", "office_a").replace('"08:12"', '"08:05"'))
    await rig.rt.reload()  # a route edit that re-seeds the outbound baseline
    assert rig.rt.state.baselines["p1|outbound"].versions == ()
    s = rig.occ()
    ctx = rig.rt._ctx(s, rig.clock.now())
    assert ctx is not None and ctx.schedule.first_departure == at("08:20")


async def test_occurrence_window_uses_the_baseline_of_its_own_service_date(tmp_path):
    rig = Rig(tmp_path, "23:25")
    rig.clock.t = at("23:25", date(2026, 10, 26))  # Monday evening
    rig.write(text=PROFILE.replace("NAME", "office_a").replace('"08:12"', '"00:20"'))
    hist = BaselineHistory((
        BaselineVersion(1, sig(time(0, 20), time(0, 50)), date(2026, 10, 1), True),
        BaselineVersion(2, sig(time(0, 30), time(0, 50)), date(2026, 10, 27), True),
    ))  # fmt: skip
    rig.store.save(PersistedState(None, {}, {"p1|outbound": hist}, {}, {}, (), True))
    rig.provider.world = []
    await rig.rt.start()
    # Tuesday's window opens 23:30 by Tuesday's 00:30, not 23:20 by Monday's 00:20
    assert rig.occ("p1|outbound|2026-10-27") is None
    rig.clock.t = at("23:30", date(2026, 10, 26))
    await rig.rt.poll_once()
    s = rig.occ("p1|outbound|2026-10-27")
    assert s is not None and s.baseline_version == 2


# --- finding 17: a look-ahead success ends the key outage ---


async def test_lookahead_success_ends_the_key_outage(tmp_path):
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = every_day()
    await rig.rt.start()
    assert not rig.rt.state.occurrences
    for err in (AuthError(), None, AuthError()):
        rig.provider.error = lambda q, err=err: err
        await rig.rt.lookahead_once()
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 2


# --- finding 1: platform changes reach the user through the runtime ---


async def test_platform_change_delivered_through_the_runtime(tmp_path):
    rig = await started(tmp_path)
    b = replace(L1.stops[0], planned_track="1", actual_track="5")
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([replace(L1, stops=(b, *L1.stops[1:])), L2])]
    await rig.rt.poll_once()
    pc = [e for e in rig.rt.events_log if e.kind is EventKind.PLATFORM_CHANGE]
    assert len(pc) == 1 and "platform 5 (was 1)" in pc[0].text


# --- finding 16: the run keeps a validated station list ---


def test_station_list_saved_and_reused(tmp_path):
    from train_commute_companion.cli import _station_list

    cache = tmp_path / "s.stations.json"
    assert _station_list(cache, None) is None  # nothing fetched, nothing saved: refuse
    assert _station_list(cache, frozenset({"AAA", "UT"})) == {"AAA", "UT"}
    assert json.loads(cache.read_text()) == ["AAA", "UT"]
    assert _station_list(cache, None) == {"AAA", "UT"}
    cache.write_text("[]")
    assert _station_list(cache, None) is None


# --- finding 15: replay times use the runtime's DST resolver ---


def test_replay_times_follow_the_core_resolver():
    from train_commute_companion.replay import ReplayError, _local

    gap = _local("2026-03-29 02:30", date.min, "t")
    assert gap.astimezone(UTC).isoformat() == "2026-03-29T01:00:00+00:00"  # 03:00 local
    first = _local("2026-10-25 02:30", date.min, "t")
    second = _local("2026-10-25 02:30+01:00", date.min, "t")
    assert first.astimezone(UTC).isoformat() == "2026-10-25T00:30:00+00:00"
    assert second.astimezone(UTC).isoformat() == "2026-10-25T01:30:00+00:00"
    with pytest.raises(ReplayError):
        _local("2026-10-25 02:30+05:00", date.min, "t")


# --- second Codex pass on the runtime group ---


def tracked(leg, planned: str, actual: str):
    b = replace(leg.stops[0], planned_track=planned, actual_track=actual)
    return replace(leg, stops=(b, *leg.stops[1:]))


async def test_platform_back_to_the_planned_track_is_told(tmp_path):
    rig = await started(tmp_path)
    for hhmm, track in (("07:52", "5"), ("07:54", "1"), ("07:56", "1"), ("07:58", "5")):
        rig.clock.set(hhmm)
        rig.provider.world = [make_connection([tracked(L1, "1", track), L2])]
        await rig.rt.poll_once()
    pc = [e.text for e in rig.rt.events_log if e.kind is EventKind.PLATFORM_CHANGE]
    assert len(pc) == 3
    assert "platform 5 (was 1)" in pc[0] and "platform 1 again" in pc[1]
    assert "platform 5 (was 1)" in pc[2]


async def test_unchanged_platform_says_nothing(tmp_path):
    rig = await started(tmp_path)
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([tracked(L1, "1", "1"), L2])]
    await rig.rt.poll_once()
    assert EventKind.PLATFORM_CHANGE not in kinds(rig.rt.events_log)


async def test_boarding_cut_short_train_keeps_its_platform_change(tmp_path):
    rig = await started(tmp_path)
    cut = make_leg("AAA", "UT", "08:12", "08:40", number="1234", stops=["BBB"], cut_after="BBB")
    rig.clock.set("07:58")  # boarding zone; nothing onward from BBB, so the train stays locked
    rig.provider.world = [make_connection([tracked(cut, "1", "4"), L2])]
    await rig.rt.poll_once()
    got = kinds(rig.rt.events_log)
    assert EventKind.CUT_SHORT in got
    pc = [e for e in rig.rt.events_log if e.kind is EventKind.PLATFORM_CHANGE]
    assert len(pc) == 1 and "platform 4 (was 1)" in pc[0].text
    assert pc[0].severity is Severity.LOUD  # within 15 min of its departure


async def test_older_store_cancel_restore_restart_cancel_alerts_twice(tmp_path):
    rig = await started(tmp_path)

    def as_older_store() -> None:  # written before `live:` markers existed
        s = rig.occ()
        keys = {k: v for k, v in s.alert_keys.items() if not k.startswith("live:")}
        rig.occ_set(replace(s, alert_keys=keys))
        rig.rt._save()

    for hhmm, world in (("07:52", [make_connection([L1X, L2]), LATER]), ("07:54", [USUAL, LATER])):
        rig.clock.set(hhmm)
        rig.provider.world = world
        await rig.rt.poll_once()
        as_older_store()
    rig.clock.set("07:56")
    rt = await rig.restart()  # reconcile poll: the cancellation is over
    rig.clock.set("07:58")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rt.poll_once()
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 2


def escape_world(escape_cancelled: bool = False) -> list:
    return [
        make_connection([make_leg("RTD", "AAA", "17:22", "18:00", number="7922",
                                  cancelled=escape_cancelled)]),
        make_connection([make_leg("RTD", "AAA", "18:22", "19:00", number="7982")]),
    ]  # fmt: skip


async def test_first_day_advice_when_the_usual_is_absent(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.provider.world = escape_world()  # no 17:52 at all and no baseline yet
    await rig.rt.start()
    s = rig.occ("p1|return|2026-10-27")
    assert s is not None and s.lock is None
    leave = [e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]
    assert len(leave) == 1 and "17:22" in leave[0].text


async def test_held_leave_now_dropped_once_its_escape_train_is_cancelled(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    assert any(o.event.kind is EventKind.LEAVE_NOW for o in rig.rt.state.obligations)
    rig.clock.set("16:56")
    rig.provider.world = escape_world(escape_cancelled=True)
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    assert EventKind.LEAVE_NOW not in kinds(rig.rt.events_log)
    assert not any(o.event.kind is EventKind.LEAVE_NOW for o in rig.rt.state.obligations)


async def test_held_leave_now_states_the_current_departure(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    rig.clock.set("16:56")
    late = make_leg("RTD", "AAA", "17:22", "18:00", number="7922", delay_dep=4, delay_arr=4)
    rig.provider.world = [make_connection([late]), *escape_world()[1:]]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    leave = [e.text for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]
    assert len(leave) == 1 and "leaves at 17:26" in leave[0]


async def test_held_all_good_dropped_once_a_train_is_cancelled(tmp_path):
    rig = Rig(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.ALL_GOOD
    await rig.rt.start()
    assert any(o.event.kind is EventKind.ALL_GOOD for o in rig.rt.state.obligations)
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    got = kinds(rig.rt.events_log)
    assert EventKind.CANCELLED in got and EventKind.ALL_GOOD not in got


async def test_held_all_good_still_sent_while_it_holds(tmp_path):
    rig = Rig(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.ALL_GOOD
    await rig.rt.start()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    assert kinds(rig.rt.events_log).count(EventKind.ALL_GOOD) == 1


async def test_held_warning_names_the_whole_onward_suffix_with_platforms(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    first = make_leg("UT", "RTD", "08:58", "09:32", number="5800")
    then = tracked(make_leg("UT", "RTD", "09:10", "09:44", number="5900"), "5", "5")
    rig.clock.set("08:20")  # the 08:12 is being ridden
    rig.provider.world = [make_connection([L1, L2X]), make_connection([L1, first]),
                          make_connection([L1, then])]  # fmt: skip
    await rig.rt.poll_once()
    held = [o.event.text for o in rig.rt.state.obligations if o.event.kind is EventKind.CANCELLED]
    assert held and "Next good connection: 08:58" in held[0]
    rig.clock.set("08:22")  # the 08:58 is cancelled too: the onward lock moves to 09:10
    first_x = make_leg("UT", "RTD", "08:58", "09:32", number="5800", cancelled=True)
    rig.provider.world = [make_connection([L1, L2X]), make_connection([L1, first_x]),
                          make_connection([L1, then])]  # fmt: skip
    await rig.rt.poll_once()
    assert [x.train.train_number for x in rig.occ().lock.legs][-1] == "5900"
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    told = [e.text for e in rig.rt.events_log if e.kind is EventKind.CANCELLED]
    old = next(t for t in told if "08:50" in t)
    assert "08:58" not in old
    assert old.endswith("Now following your 08:12 train, then the 09:10 from UT (platform 5).")


async def test_older_store_occurrence_keeps_its_schedule_through_a_reseed(tmp_path):
    rig = Rig(tmp_path, "07:56")
    hist = BaselineHistory((BaselineVersion(1, sig(time(8, 20), time(8, 50)), date(2026, 10, 1),
                            True),))  # fmt: skip
    rig.store.save(PersistedState(None, {}, {"p1|outbound": hist}, {}, {}, (), True))
    rig.provider.world = every_day("08:20")
    await rig.rt.start()
    s = rig.occ()
    rig.occ_set(replace(s, baseline_signature=None, baseline_frozen=False))  # an older store
    rig.rt._save()
    rt = await rig.restart()
    rig.write(text=PROFILE.replace("NAME", "office_a").replace('"08:12"', '"08:05"'))
    await rt.reload()  # a route edit that re-seeds the outbound baseline
    assert rt.state.baselines["p1|outbound"].versions == ()
    ctx = rt._ctx(rig.occ(), rig.clock.now())
    assert ctx is not None and ctx.schedule.first_departure == at("08:20")


async def test_occurrence_started_without_a_baseline_never_takes_a_later_one(tmp_path):
    rig = await started(tmp_path)
    s = rig.occ()
    assert s.baseline_frozen and s.baseline_signature is None
    hist = BaselineHistory((BaselineVersion(1, sig(time(8, 20), time(8, 50)), date(2026, 10, 1),
                            True),))  # fmt: skip
    rig.rt._st = replace(rig.rt._st, baselines={"p1|outbound": hist})
    ctx = rig.rt._ctx(rig.occ(), rig.clock.now())
    assert ctx is not None and ctx.schedule.first_departure == at("08:12")


async def test_falling_delay_still_past_the_limit_stays_loud(tmp_path):
    rig = Rig(tmp_path)
    limit = '    alerts: {latest_acceptable_arrival: "09:24", quiet_hours: ["07:00", "10:00"]}\n'
    rig.write(text=PROFILE.replace("NAME", "office_a").replace(
        '    return: {first_train: "17:52"', limit + '    return: {first_train: "17:52"', 1))  # fmt: skip
    await rig.rt.start()
    rig.notifier.fails = lambda e: e.kind is EventKind.DELAYED
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1, late_l2(7)])]  # arrives 09:31, limit 09:24
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    rig.clock.set("07:54")
    rig.provider.world = [make_connection([L1, late_l2(1)])]  # 09:25: still past the limit
    await rig.rt.poll_once()
    told = [e for e in rig.rt.events_log if e.kind in (EventKind.DELAYED, EventKind.BACK_TO_NORMAL)]
    assert [(e.kind, e.severity) for e in told] == [(EventKind.DELAYED, Severity.LOUD)]
    assert "delayed 1 min" in told[0].text


async def test_delay_memory_is_every_value_the_message_stated(tmp_path):
    rig = await started(tmp_path)
    for hhmm, (dep, arr) in (("07:52", (7, 7)), ("07:54", (6, 10)), ("07:56", (9, 10))):
        rig.clock.set(hhmm)
        leg = make_leg("UT", "RTD", "08:50", "09:24", number="5678", delay_dep=dep,
                       delay_arr=arr)  # fmt: skip
        rig.provider.world = [make_connection([L1, leg])]
        await rig.rt.poll_once()
    delayed = [e.text for e in rig.rt.events_log if e.kind is EventKind.DELAYED]
    assert len(delayed) == 3
    assert "leaves 6 min late and arrives 10 min late" in delayed[1]
    assert "leaves 9 min late" in delayed[2]


class NoStations(Exception):
    pass


async def test_no_station_list_runs_the_saved_configuration_and_waits_on_edits(tmp_path):
    from train_commute_companion.providers.base import ProviderError

    rig = await started(tmp_path)  # a validated configuration in the store
    rt = rig.new()
    rt.require_stations = True
    saved: list[frozenset[str]] = []
    rt.on_stations = saved.append

    async def down() -> frozenset[str]:
        raise ProviderError("station endpoint down")

    rig.provider.stations = down  # type: ignore[method-assign]
    rig.rt = rt
    await rt.start()
    assert rt.config is not None and rig.occ() is not None and rig.occ().lock is not None
    assert EventKind.PROFILE_REFUSED not in kinds(rt.events_log)  # nothing unchecked to refuse
    rig.write("office_x")
    await rt.check_file()
    assert rt.config.profiles[0].name == "office_a"
    assert kinds(rt.events_log).count(EventKind.PROFILE_REFUSED) == 1
    await rt.check_file()  # retried only once per poll interval: no second request or warning
    assert kinds(rt.events_log).count(EventKind.PROFILE_REFUSED) == 1

    async def up() -> frozenset[str]:
        return frozenset({"AAA", "UT", "RTD"})

    rig.provider.stations = up  # type: ignore[method-assign]
    rig.clock.jump(2)
    await rt.check_file()
    assert rt.config.profiles[0].name == "office_x" and saved == [frozenset({"AAA", "UT", "RTD"})]


def test_run_starts_without_a_station_list(tmp_path, monkeypatch):
    from train_commute_companion import cli

    ran: list[cli.Runtime] = []

    async def none(self) -> None:
        return None

    async def forever(self, *, started: bool = False) -> None:
        ran.append(self)

    monkeypatch.setattr(cli.Runtime, "stations", none)
    monkeypatch.setattr(cli.Runtime, "run_forever", forever)
    monkeypatch.setenv("NS_API_KEY", "k")
    store = tmp_path / "state.json"
    assert cli.main(["run", str(tmp_path / "profiles.yaml"), "--store", str(store)]) == 0
    assert len(ran) == 1 and ran[0].require_stations and ran[0].known_stations is None


async def test_held_back_to_normal_dropped_once_the_delay_returns(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.BACK_TO_NORMAL
    for hhmm, late in (("07:52", 7), ("07:54", 0), ("07:56", 7)):
        rig.clock.set(hhmm)
        rig.provider.world = [make_connection([L1, late_l2(late)])]
        await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    got = kinds(rig.rt.events_log)
    assert got.count(EventKind.DELAYED) == 2 and EventKind.BACK_TO_NORMAL not in got


async def test_held_restored_dropped_once_the_train_is_cancelled_again(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.RESTORED
    for hhmm, world in (("07:52", [make_connection([L1X, L2]), LATER]),
                        ("07:54", [USUAL, LATER]),
                        ("07:56", [make_connection([L1X, L2]), LATER])):  # fmt: skip
        rig.clock.set(hhmm)
        rig.provider.world = world
        await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    got = kinds(rig.rt.events_log)
    assert got.count(EventKind.CANCELLED) == 2 and EventKind.RESTORED not in got


# --- final review MUST_FIX 1: a held all-clear never follows a cancellation ---

L2_CUT = make_leg("UT", "RTD", "08:50", "09:24", number="5678", stops=["GD"], cut_after="GD")


@pytest.mark.parametrize("hold", ["failure", "quiet_hours"])
@pytest.mark.parametrize("broken", [L2X, L2_CUT], ids=["cancelled", "cut_short"])
async def test_held_back_to_normal_dropped_after_cancellation_without_replacement(
    tmp_path, hold, broken
):
    rig = Rig(tmp_path)
    if hold == "quiet_hours":
        quiet = '    alerts: {quiet_hours: ["07:51", "07:58"]}\n'
        rig.write(text=PROFILE.replace("NAME", "office_a").replace(
            '    return: {first_train: "17:52"', quiet + '    return: {first_train: "17:52"', 1))  # fmt: skip
    else:
        rig.notifier.fails = lambda e: e.kind is EventKind.BACK_TO_NORMAL
    await rig.rt.start()
    await rig.rt.poll_once()
    for hhmm, world in (("07:52", [make_connection([L1, late_l2(5)])]),
                        ("07:54", [make_connection([L1, late_l2(0)])])):  # fmt: skip
        rig.clock.set(hhmm)
        rig.provider.world = world
        await rig.rt.poll_once()
    assert any(o.event.kind is EventKind.BACK_TO_NORMAL for o in rig.rt.state.obligations)
    rig.clock.set("07:56")
    rig.provider.world = [make_connection([L1, broken])]  # and no connection onward
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    rig.clock.set("07:59")
    await rig.rt.watchdog_once()
    assert EventKind.BACK_TO_NORMAL not in kinds(rig.rt.events_log)
    assert not any(o.event.kind is EventKind.BACK_TO_NORMAL for o in rig.rt.state.obligations)


# --- final review MUST_FIX 2: a failed works lookup still probes, a missed run is caught up ---


@pytest.mark.parametrize("err", [TimeoutError(), MalformedAnswer("bad json")])
async def test_failed_works_lookup_still_probes_and_warns(tmp_path, err):
    rig = Rig(tmp_path, "05:40")
    tomorrow = DAY + timedelta(days=1)
    l2x = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=tomorrow, cancelled=True)
    rig.provider.world = [
        *(c for c in every_day() if c.legs[0].train.operating_date != tomorrow),
        make_connection([make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=tomorrow),
                         l2x]),
        *returns_every_day("17:52"),
    ]  # fmt: skip
    await rig.rt.start()

    async def fail() -> object:
        raise err

    rig.provider.planned_works = fail  # type: ignore[method-assign]
    await rig.rt.lookahead_once()
    assert rig.provider.calls, "the probes ran"
    works = [
        e for e in rig.rt.events_log if e.kind is EventKind.WORKS_AHEAD and e.profile_id == "p1"
    ]
    assert [(e.direction.value, e.severity) for e in works] == [("outbound", Severity.LOUD)]
    assert "Wednesday 28 Oct" in works[0].text
    assert rig.rt.state.baselines.get("p1|outbound", BaselineHistory()).versions == ()
    assert rig.rt.state.lookahead["p1|outbound"].last_run is None  # a failed run for health


class Sleeps:
    """A clock sleep for `run_forever`: moves time at most `step` seconds per call, and stops
    the run at a sleep past an hour (the wait for tomorrow's run), after `limit` calls or once
    `done()` holds."""

    def __init__(self, rig: Rig, step: float, limit: int, done=lambda: False) -> None:
        self.rig, self.step, self.limit, self.done, self.n = rig, step, limit, done, 0

    async def __call__(self, seconds: float) -> None:
        self.n += 1
        await asyncio.sleep(0)
        if seconds > 3600 or self.n > self.limit or self.done():
            raise Stop
        self.rig.clock.jump(min(seconds, self.step) / 60)


async def test_lookahead_catches_up_after_start_past_six(tmp_path):
    rig = Rig(tmp_path, "07:00")
    rig.clock.sleep = Sleeps(rig, 0, 40)  # type: ignore[method-assign]
    with pytest.raises(Stop):
        await rig.rt.run_forever()
    assert rig.provider.works_calls == 1


async def test_lookahead_retried_each_poll_interval_until_a_run_completes(tmp_path):
    rig = Rig(tmp_path, "06:00")
    plain = rig.provider.planned_works

    async def first_fails() -> object:
        if rig.provider.works_calls == 0:
            rig.provider.works_calls += 1
            raise ServerError("503")  # also starts the shared cooldown: the probes wait
        return await plain()

    rig.provider.planned_works = first_fails  # type: ignore[method-assign]
    led = lambda: rig.rt.state.lookahead.get("p1|outbound")
    rig.clock.sleep = Sleeps(rig, 30, 400, lambda: led() is not None)  # type: ignore[method-assign]
    with pytest.raises(Stop):
        await rig.rt.run_forever()
    assert rig.provider.works_calls == 2
    assert led().last_applied < at("06:10")


# --- codex whole-plan review: a retained journey is told from its own position (rule 7) ---

DIRECT = make_leg("AAA", "RTD", "08:25", "09:10", number="1400")


async def test_retained_cancel_names_its_own_replacement(tmp_path):
    rig = Rig(tmp_path, "08:00")
    await rig.rt.start()
    await rig.rt.poll_once()
    s = rig.occ()
    rig.occ_set(replace(s, lock=journey([DIRECT]), retained=(journey([L1, L2], RIDE),),
                        direction_state=DirectionState.RIDING))  # fmt: skip
    rig.provider.world = [make_connection([L1, L2X]), make_connection([DIRECT]),
                          make_connection([R2])]  # fmt: skip
    rig.clock.set("08:20")
    await rig.rt.poll_once()
    sent = [e.text for e in rig.notifier.attempts if e.kind is EventKind.CANCELLED]
    # someone on the 08:12 cannot catch the 08:25 from AAA: the next good one is from UT
    assert sent and "09:05" in sent[0] and "08:25" not in sent[0]


async def test_retained_journey_back_to_normal_is_delivered(tmp_path):
    rig = Rig(tmp_path, "08:00")
    await rig.rt.start()
    await rig.rt.poll_once()
    s = rig.occ()
    # the retained direct train was cut short at UT and gained a recovery leg from there
    j = journey([T_FULL, FROM_UT], RIDE)
    cut = replace(j.legs[0], served_endpoint="UT", alight_stop="UT")
    rig.occ_set(replace(s, lock=journey([DIRECT]), retained=(replace(j, legs=(cut, j.legs[1])),),
                        direction_state=DirectionState.RIDING))  # fmt: skip
    rig.provider.world = [make_connection([T_FULL]), make_connection([DIRECT]),
                          make_connection([FROM_UT])]  # fmt: skip
    rig.clock.set("08:20")  # it reaches RTD again before the recovery leaves
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    sent = [e for e in rig.notifier.attempts if e.kind is EventKind.RESTORED]
    assert sent and ":retained:1234@" in sent[0].key


async def test_held_all_good_names_the_current_platform(tmp_path):
    rig = Rig(tmp_path, "07:50")
    rig.provider.world = [make_connection([tracked(L1, "1", "1"), L2])]
    rig.notifier.fails = lambda e: e.kind is EventKind.ALL_GOOD
    await rig.rt.start()
    await rig.rt.poll_once()
    rig.clock.set("07:52")  # the platform moves to 5 and that change is delivered
    rig.provider.world = [make_connection([tracked(L1, "1", "5"), L2])]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    got = [(e.kind, e.text) for e in rig.notifier.attempts]
    moved = next(i for i, (k, _) in enumerate(got) if k is EventKind.PLATFORM_CHANGE)
    last = max(i for i, (k, _) in enumerate(got) if k is EventKind.ALL_GOOD)
    assert last > moved and "platform 1" not in got[last][1] and "platform 5" in got[last][1]


async def test_window_edit_keeps_an_undelivered_works_warning_told_later(tmp_path):
    rig = Rig(tmp_path, "05:40")
    tomorrow = DAY + timedelta(days=1)
    l2x = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=tomorrow, cancelled=True)
    rig.provider.world = [
        *(c for c in every_day() if c.legs[0].train.operating_date != tomorrow),
        make_connection(
            [make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=tomorrow), l2x]
        ),
        *returns_every_day("17:52"),
    ]
    rig.notifier.fails = lambda e: e.kind is EventKind.WORKS_AHEAD
    await rig.rt.start()
    await rig.rt.lookahead_once()
    assert any(o.event.kind is EventKind.WORKS_AHEAD for o in rig.rt.state.obligations)
    # a window-only route edit: rule 5(c) drops the held warning, the baseline is kept
    rig.write(text=PROFILE.replace("NAME", "office_a").replace(
        "transfers: [UT]}", 'transfers: [UT], window: ["06:42", "10:12"]}', 1))  # fmt: skip
    await rig.rt.reload()
    assert rig.rt.config.revision == 2
    rig.notifier.fails = lambda e: False
    rig.notifier.attempts.clear()
    await rig.rt.watchdog_once()
    rig.clock.set("12:00")
    await rig.rt.lookahead_once()
    await rig.rt.watchdog_once()
    got = [e.text for e in rig.notifier.attempts if e.kind is EventKind.WORKS_AHEAD]
    assert any("28 Oct" in t for t in got)

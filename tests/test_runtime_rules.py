"""Runtime safety rules end to end: news episodes, escalation, rule 5 delivery, location and
look-ahead writers, the shared budget, the poll deadline, baselines and health (7.1, 7.11,
7.12 rules 4, 5, 10, 11, 12, 13, 14, 21)."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, time, timedelta

import pytest
from builders import make_connection, make_leg
from test_lifecycle_follow import L1, L1X, L2, LATER
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
from train_commute_companion.lifecycle import NotTravelling
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
from train_commute_companion.providers.base import AuthError, Query, RateLimited
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

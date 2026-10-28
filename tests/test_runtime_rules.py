"""Runtime safety rules end to end: news episodes, escalation, rule 5 delivery, location and
look-ahead writers, the shared budget, the poll deadline, baselines and health (7.1, 7.11,
7.12 rules 4, 5, 10, 11, 12, 13, 14, 21)."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, time, timedelta

import pytest
from builders import _on_track, make_connection, make_leg, planned_at_alight
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
from train_commute_companion.lookahead import BaselineHistory, BaselineVersion, render_message
from train_commute_companion.model import (
    AlertEvent,
    Direction,
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
from train_commute_companion.serde import dumps, loads
from train_commute_companion.store import PersistedState

OUT = Direction.OUTBOUND


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


@pytest.fixture
def inline_file_reads(monkeypatch):
    """For tests that count instant fake sleeps: the profile read runs inline, since a real
    thread hop lets the watchdog's zero-time sleeps spin (and jump the clock) while it reads."""

    async def inline(fn, /, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


@pytest.mark.usefixtures("inline_file_reads")
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
        rig.clock.jump(4)  # past a rejection's 2-min cooldown plus the look-ahead's extra interval
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


async def test_data_unclear_through_the_runtime_survives_a_restart(tmp_path):
    rig = await started(tmp_path)
    split = [make_connection([planned_at_alight(L1), L2]), make_connection([L1, L2])]
    moved = [
        make_connection([planned_at_alight(L1), L2]),
        make_connection([_on_track(L1, "7"), L2]),
    ]
    for hhmm, world in (("07:52", [USUAL]), ("07:54", split), ("07:56", split),
                        ("07:58", [USUAL]), ("08:00", split), ("08:02", moved)):  # fmt: skip
        rig.clock.set(hhmm)
        rig.provider.world = world
        await rig.rt.poll_once()
    sent = [e for e in rig.rt.events_log if e.kind is EventKind.DATA_UNCLEAR]
    assert [e.severity for e in sent] == [Severity.LOUD, Severity.LOUD]  # 07:54 and 08:02
    await rig.restart()
    rig.clock.set("08:04")
    await rig.rt.poll_once()  # world still `moved`
    assert EventKind.DATA_UNCLEAR not in kinds(rig.rt.events_log)


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


async def test_platform_back_to_planned_without_a_live_track_is_told(tmp_path):
    # NS can drop the live departure track once the train is back on its planned one
    rig = await started(tmp_path)
    for hhmm, track in (("07:52", "5"), ("07:54", None), ("07:56", None)):
        rig.clock.set(hhmm)
        rig.provider.world = [make_connection([tracked(L1, "1", track), L2])]
        await rig.rt.poll_once()
    pc = [e.text for e in rig.rt.events_log if e.kind is EventKind.PLATFORM_CHANGE]
    assert len(pc) == 2
    assert "platform 5 (was 1)" in pc[0] and "platform 1 again" in pc[1]


async def test_no_live_track_on_an_unchanged_platform_says_nothing(tmp_path):
    rig = await started(tmp_path)
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([tracked(L1, "1", None), L2])]
    await rig.rt.poll_once()
    assert EventKind.PLATFORM_CHANGE not in kinds(rig.rt.events_log)


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


async def test_held_leave_now_is_dropped_when_its_saving_is_gone(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    rig.clock.set("16:56")
    # the usual stays absent (bad); the escape train now arrives 19:06, after waiting's 19:00
    slow = make_leg("RTD", "AAA", "17:22", "18:00", number="7922", delay_arr=66)
    rig.provider.world = [make_connection([slow]), *escape_world()[1:]]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    assert EventKind.LEAVE_NOW not in kinds(rig.rt.events_log)
    assert not any(o.event.kind is EventKind.LEAVE_NOW for o in rig.rt.state.obligations)


@pytest.mark.parametrize("restart", [False, True])
async def test_dropped_leave_now_is_advised_again_when_its_saving_returns(tmp_path, restart):
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    rig.clock.set("16:56")
    slow = make_leg("RTD", "AAA", "17:22", "18:00", number="7922", delay_arr=66)
    rig.provider.world = [make_connection([slow]), *escape_world()[1:]]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()  # dropped unsent: the saving is gone
    a = rig.occ("p1|return|2026-10-27").advice
    assert a.withdrawn and not a.delivered
    rt = await rig.restart() if restart else rig.rt
    rig.clock.set("16:58")
    rig.provider.world = escape_world()  # the saving is back
    await rt.poll_once()
    leave = [e for e in rig.notifier.attempts if e.kind is EventKind.LEAVE_NOW]
    assert leave and leave[-1] in rt.events_log and "17:22" in leave[-1].text
    a = rig.occ("p1|return|2026-10-27").advice
    assert a.delivered and not a.withdrawn


async def test_untold_advice_is_not_withdrawn_when_waiting_is_fine_again(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()
    rig.clock.set("16:56")
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952")]),
        *escape_world(),
    ]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    got = kinds(rig.notifier.attempts)
    assert EventKind.EARLIER_TRAIN_WITHDRAWN not in got
    assert not any(o.event.kind is EventKind.LEAVE_NOW for o in rig.rt.state.obligations)
    assert rig.occ("p1|return|2026-10-27").advice.withdrawn


async def test_held_leave_now_for_a_delayed_train_still_goes_out(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    late = make_leg("RTD", "AAA", "17:22", "18:00", number="7922", delay_dep=6, delay_arr=6)
    rig.provider.world = [make_connection([late]), *escape_world()[1:]]
    rig.clock.set("17:19")  # planned 17:22 minus the 5 min buffer, plus 2 min; leaves 17:28
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    leave = [e.text for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]
    assert len(leave) == 1 and "leaves at 17:28" in leave[0]


async def test_stored_leave_now_loses_its_frozen_expiry_on_load(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    st = rig.rt.state  # an older store froze the planned departure minus buffer as expiry
    frozen = tuple(replace(o, expires_at=at("17:17")) for o in st.obligations)
    rig.store.save(replace(st, obligations=frozen))
    late = make_leg("RTD", "AAA", "17:22", "18:00", number="7922", delay_dep=6, delay_arr=6)
    rig.provider.world = [make_connection([late]), *escape_world()[1:]]
    rig.clock.set("17:19")
    rt = await rig.restart()
    rig.notifier.fails = lambda e: False
    await rt.watchdog_once()
    leave = [e.text for e in rig.notifier.attempts if e.kind is EventKind.LEAVE_NOW]
    assert leave[-1:] and "leaves at 17:28" in leave[-1]


def cancelled_usual_world() -> list:
    return [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952",
                                  cancelled=True)]),
        *escape_world(),
    ]  # fmt: skip


async def test_advice_searches_before_the_usual_departure(tmp_path):
    # the provider answers only from the asked time on, like NS: the 17:22 is not in the answer
    # asked from the usual 17:52, so advice asks again from now plus the buffer (7.6, rule 17)
    rig = Rig(tmp_path, "16:54")
    rig.provider.window = timedelta(hours=1)
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()
    leave = [e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]
    assert len(leave) == 1 and "17:22" in leave[0].text
    assert at("16:59") in [q.at for q in rig.provider.calls]


async def test_held_advice_is_revalidated_on_the_earlier_search(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.provider.window = timedelta(hours=1)
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()
    rig.clock.set("16:56")
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    leave = [e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]
    assert len(leave) == 1 and "17:22" in leave[0].text


async def test_no_earlier_search_while_waiting_is_fine(tmp_path):
    rig = Rig(tmp_path, "16:54")
    rig.provider.window = timedelta(hours=1)
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952")]),
        *escape_world(),
    ]
    await rig.rt.start()
    assert at("16:59") not in [q.at for q in rig.provider.calls]
    assert EventKind.LEAVE_NOW not in kinds(rig.rt.events_log)


def running_usual_world() -> list:
    return [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952")]),
        *escape_world(),
    ]


async def test_newer_cancellation_on_the_advice_search_beats_an_older_recovery(tmp_path):
    # the poll's own answer shows the usual 17:52 running again, the advice search answered a
    # minute later shows it cancelled: the newer fact counts, the held advice still goes (7.6)
    rig = Rig(tmp_path, "16:54")
    rig.provider.window = timedelta(hours=1)
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()

    def split(q: Query) -> None:
        if q.at == at("17:52"):
            rig.provider.world = running_usual_world()
        else:
            rig.clock.jump(1)
            rig.provider.world = cancelled_usual_world()

    rig.clock.set("16:56")
    rig.provider.hook = split
    await rig.rt.poll_once()
    rig.provider.hook = None
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    leave = [e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]
    assert len(leave) == 1 and "17:22" in leave[0].text


# --- advice reads the current copy of every train (6.2, 7.6) ---


def test_origin_answer_keeps_distinct_connections_sharing_an_onward_train():
    from test_lifecycle_follow import ans, conn
    from test_lifecycle_follow import ctx as follow_ctx

    from train_commute_companion.lifecycle import _Pool
    from train_commute_companion.runtime import _origin_answer

    older = ans("AAA", conn(L1, R2), when="07:50")  # the 08:12 and the 08:25 share the 09:05
    newer = ans("AAA", LATER, when="07:52")
    transfer = ans("UT", conn(R2), when="07:53")
    got = _origin_answer((older, newer, transfer), follow_ctx(at("07:53")))
    assert isinstance(got, _Pool)
    assert got.connections == (LATER, conn(L1, R2))  # the newer answer's first
    assert got.answers == (older, newer, transfer)  # every answer of the poll is evidence
    assert got.query == older.query and got.fetched_at == newer.fetched_at


async def held_then_split(tmp_path, main: list, search: list) -> list[AlertEvent]:
    """A LEAVE_NOW for the 17:22 is held; the next poll's own answer shows `main`, the advice
    search answered a minute later shows `search`. The LEAVE_NOW events sent afterwards."""
    rig = Rig(tmp_path, "16:54")
    rig.provider.window = timedelta(hours=1)
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()

    def split(q: Query) -> None:
        if q.at == at("17:52"):
            rig.provider.world = main
        else:
            rig.clock.jump(1)
            rig.provider.world = search

    rig.clock.set("16:56")
    rig.provider.hook = split
    await rig.rt.poll_once()
    rig.provider.hook = None
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    return [e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]


async def test_advice_goes_when_the_newer_answer_cancels_the_usual_and_an_older_copy_arrives_earlier(
    tmp_path,
):
    early = make_leg("RTD", "AAA", "17:52", "18:30", number="7952", delay_arr=-1)  # 18:29
    leave = await held_then_split(
        tmp_path, [make_connection([early]), *escape_world()], cancelled_usual_world()
    )
    assert len(leave) == 1 and "17:22" in leave[0].text


async def test_advice_goes_when_the_newer_answer_cuts_the_usual_short(tmp_path):
    full = make_leg("RTD", "AAA", "17:52", "18:30", number="7952", stops=["UT"])
    cut = make_leg("RTD", "AAA", "17:52", "18:30", number="7952", stops=["UT"], cut_after="UT")
    leave = await held_then_split(
        tmp_path,
        [make_connection([full]), *escape_world()],
        [make_connection([cut]), *escape_world()],
    )
    assert len(leave) == 1 and "17:22" in leave[0].text


async def test_advice_goes_when_the_cut_short_copy_omits_the_destination(tmp_path):
    from builders import _cut

    full = make_leg("RTD", "AAA", "17:52", "18:30", number="7952", stops=["BBB", "UT"])
    # the newer copy lists BBB cancelled and ends at UT: AAA is not in it at all
    short = make_leg("RTD", "UT", "17:52", "18:20", number="7952", stops=["BBB"])
    short = replace(short, stops=(short.stops[0], _cut(short.stops[1]), short.stops[2]))
    onward = make_leg("UT", "AAA", "18:25", "18:35", number="7999")
    leave = await held_then_split(
        tmp_path,
        [make_connection([full]), *escape_world()],
        [make_connection([short, onward]), *escape_world()],
    )
    assert len(leave) == 1 and "17:22" in leave[0].text


async def test_leave_now_withdrawn_when_a_newer_answer_cancels_it(tmp_path):
    gone = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952", cancelled=True)]),
        *escape_world(escape_cancelled=True),
    ]
    assert await held_then_split(tmp_path, cancelled_usual_world(), gone) == []


async def test_advice_saving_uses_the_current_arrival(tmp_path):
    def world(escape_late: int) -> list:
        return [
            make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952",
                                      delay_arr=12)]),  # waiting arrives 18:42
            make_connection([make_leg("RTD", "AAA", "17:22", "18:00", number="7922",
                                      delay_arr=escape_late)]),
        ]  # fmt: skip

    rig = Rig(tmp_path, "16:54")
    rig.provider.hook = lambda q: (
        None
        if q.at == at("17:52")
        else (rig.clock.jump(1), setattr(rig.provider, "world", world(50)))
    )
    rig.provider.world = world(30)  # the poll's own answer: the 17:22 arrives 18:30
    await rig.rt.start()  # the advice search a minute later: it arrives 18:50
    assert at("16:59") in [q.at for q in rig.provider.calls]
    assert EventKind.LEAVE_NOW not in kinds(rig.rt.events_log)


async def test_advice_never_offers_an_onward_train_a_transfer_answer_cancelled(tmp_path):
    rig = Rig(tmp_path)
    rig.write(text=PROFILE.replace("NAME", "office_a").replace(
        "transfers: [UT]}", "transfers: [UT], earlier_train_advice: true}"))  # fmt: skip
    rig.rt = rig.new()
    early_l1 = make_leg("AAA", "UT", "08:04", "08:30", number="1300")
    onward = make_leg("UT", "RTD", "08:36", "09:10", number="6666")
    late = make_connection([make_leg("AAA", "UT", "08:44", "09:12", number="7777"),
                            make_leg("UT", "RTD", "09:20", "09:54", number="8888")])  # fmt: skip

    def world(onward_cancelled: bool) -> list:  # the usual 08:12 is cancelled
        gone = make_leg("UT", "RTD", "08:36", "09:10", number="6666", cancelled=True)
        mid = make_connection([early_l1, gone if onward_cancelled else onward])
        return [make_connection([L1X, L2]), mid, late]

    rig.provider.world = [USUAL, make_connection([early_l1, onward]), late]
    await rig.rt.start()
    assert rig.occ().lock.legs[0].train == L1.train
    rig.clock.set("07:52")

    def split(q: Query) -> None:
        if q.origin == "UT":  # asked from the transfer station, after the origin answer
            rig.clock.jump(1 / 6)
            rig.provider.world = world(True)
        else:
            rig.provider.world = world(False)

    rig.provider.hook = split
    # the advice search fails: advice has only the poll's own answers
    rig.provider.error = lambda q: ServerError() if q.at == at("08:02") else None
    await rig.rt.poll_once()
    assert [q.origin for q in rig.provider.calls[-3:]] == ["AAA", "UT", "AAA"]
    assert EventKind.LEAVE_NOW not in kinds(rig.rt.events_log)
    assert [x.train.train_number for x in rig.occ().lock.legs] == ["7777", "8888"]


async def test_advice_search_shares_the_poll_deadline(tmp_path):
    rig = Rig(tmp_path, "16:54")  # 7.1: one hard deadline for the poll, not one per request
    rig.provider.window = timedelta(hours=1)
    rig.provider.world = cancelled_usual_world()
    rig.provider.hook = lambda q: rig.clock.jump(70 / 60)
    await rig.rt.start()  # 70 s for the poll's own request, 70 s more for the advice search
    assert len(rig.provider.calls) == 2
    assert EventKind.LEAVE_NOW not in kinds(rig.rt.events_log)  # the late answer is not used


async def test_rejected_key_on_the_advice_search_is_reported(tmp_path):
    rig = Rig(tmp_path, "16:54")  # 7.11
    rig.provider.window = timedelta(hours=1)
    rig.provider.world = cancelled_usual_world()
    rig.provider.error = lambda q: None if q.at == at("17:52") else AuthError()
    await rig.rt.start()
    assert rig.rt.key_rejected
    assert EventKind.KEY_REJECTED in kinds(rig.rt.events_log)
    rec = rig.occ("p1|return|2026-10-27").poll_records[-1]
    assert (rec.ok, rec.error) == (False, "auth")


async def test_skip_written_during_a_send_drops_the_next_message(tmp_path):
    rig = Rig(tmp_path, "16:54")  # 7.8, rule 5: each message is judged on the state now
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    rt = rig.rt
    rt._app_warning(EventKind.KEY_REJECTED, "your NS key was rejected.", rig.clock.now())
    st = rt._st
    rt._st = replace(st, obligations=(st.obligations[-1], *st.obligations[:-1]))
    inner = rig.notifier.send
    skip: list[asyncio.Future[bool]] = []

    async def send(e: AlertEvent) -> bool:
        if e.kind is EventKind.KEY_REJECTED:  # "not travelling" is written while this sends
            skip.append(asyncio.ensure_future(rt.act("p1", NotTravelling(DAY, False))))
            for _ in range(5):
                await asyncio.sleep(0)
            assert rig.occ("p1|return|2026-10-27").not_travelling
        return await inner(e)

    rig.notifier.send = send  # type: ignore[method-assign]
    rig.notifier.fails = lambda e: False
    await rt.watchdog_once()
    await skip[0]
    assert EventKind.KEY_REJECTED in kinds(rt.events_log)
    assert EventKind.LEAVE_NOW not in kinds(rt.events_log)


@pytest.mark.parametrize("restart", [False, True])
async def test_advice_dropped_while_the_store_fails_is_advised_again(tmp_path, restart):
    rig = Rig(tmp_path, "16:54")  # rule 22, 7.6
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = escape_world()
    await rig.rt.start()
    rig.store.fail = True
    rig.clock.set("16:56")
    slow = make_leg("RTD", "AAA", "17:22", "18:00", number="7922", delay_arr=66)
    rig.provider.world = [make_connection([slow]), *escape_world()[1:]]
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()  # dropped unsent while nothing can be written
    rig.store.fail = False
    await rig.rt.watchdog_once()
    a = rig.store.load().occurrences["p1|return|2026-10-27"].advice
    assert a.withdrawn and not a.delivered
    rt = await rig.restart() if restart else rig.rt
    rig.clock.set("16:58")
    rig.provider.world = escape_world()  # the saving is back
    await rt.poll_once()
    leave = [e for e in rig.notifier.attempts if e.kind is EventKind.LEAVE_NOW]
    assert leave and leave[-1] in rt.events_log and "17:22" in leave[-1].text


async def test_advice_sent_while_the_store_fails_is_withdrawn_later(tmp_path):
    rig = Rig(tmp_path, "16:54")  # rule 22, 7.6
    rig.store.fail = True
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()  # the LEAVE_NOW goes from memory
    assert EventKind.LEAVE_NOW in kinds(rig.rt.events_log)
    rig.store.fail = False
    await rig.rt.watchdog_once()
    assert rig.store.load().occurrences["p1|return|2026-10-27"].advice.delivered
    rig.clock.set("16:56")
    rig.provider.world = running_usual_world()
    await rig.rt.poll_once()
    assert EventKind.EARLIER_TRAIN_WITHDRAWN in kinds(rig.rt.events_log)


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


@pytest.mark.usefixtures("inline_file_reads")
async def test_lookahead_catches_up_after_start_past_six(tmp_path):
    rig = Rig(tmp_path, "07:00")
    rig.clock.sleep = Sleeps(rig, 0, 40)  # type: ignore[method-assign]
    with pytest.raises(Stop):
        await rig.rt.run_forever()
    assert rig.provider.works_calls == 1


@pytest.mark.usefixtures("inline_file_reads")
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


# --- rule 5(b): a held look-ahead message that no longer holds is dropped ---


def _world_with(day: date, l2: object) -> list:
    """Every usual journey, the outbound on `day` with `l2` as its second leg."""
    l1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=day)
    return [
        *(c for c in every_day() if c.legs[0].train.operating_date != day),
        make_connection([l1, l2]),
        *returns_every_day("17:52"),
    ]


def _cancelled_l2(day: date) -> list:
    l2x = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=day, cancelled=True)
    return _world_with(day, l2x)


def _clean() -> list:
    return [*every_day(), *returns_every_day("17:52")]


def _ahead(rig: Rig, kind: EventKind) -> list[AlertEvent]:
    return [e for e in rig.notifier.attempts if e.kind is kind and e.profile_id == "p1"]


async def _held_warning(tmp_path, day: date) -> Rig:
    """05:40 on DAY: the look-ahead tells works on `day`; every delivery fails."""
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = _cancelled_l2(day)
    rig.notifier.fails = lambda e: True
    await rig.rt.start()
    await rig.rt.lookahead_once()
    assert any(o.event.kind is EventKind.WORKS_AHEAD for o in rig.rt.state.obligations)
    return rig


async def _back(rig: Rig) -> None:
    rig.notifier.fails = lambda e: False
    rig.notifier.attempts.clear()
    await rig.rt.watchdog_once()


NEXT = DAY + timedelta(days=1)  # the look-ahead's next commute day, as `_held_warning` uses it


def _split_world(day: date, other) -> list:
    l1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=day)
    l2 = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=day)
    return [
        *(c for c in every_day() if c.legs[0].train.operating_date != day),
        make_connection([planned_at_alight(l1), l2]),
        make_connection([other(l1), l2]),
        *returns_every_day("17:52"),
    ]


async def test_superseded_lookahead_data_unclear_is_never_delivered(tmp_path):
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = _split_world(NEXT, lambda leg: leg)  # A: planned only vs live
    rig.notifier.fails = lambda e: True
    await rig.rt.start()
    await rig.rt.lookahead_once()
    rig.clock.set("06:10")
    rig.provider.world = _split_world(NEXT, lambda leg: _on_track(leg, "7"))  # B
    await rig.rt.lookahead_once()
    await _back(rig)
    (e,) = _ahead(rig, EventKind.DATA_UNCLEAR)
    assert "platform 7" in e.text


async def test_lookahead_and_live_data_unclear_are_separate(tmp_path):
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = _split_world(NEXT, lambda leg: leg)
    await rig.rt.start()
    await rig.rt.lookahead_once()  # told ahead for NEXT by the look-ahead
    (ahead,) = _ahead(rig, EventKind.DATA_UNCLEAR)
    assert f"{NEXT:%A}" in ahead.text  # names the day
    for hhmm in ("07:52", "07:54", "07:56"):  # the first poll creates the lock; live checks after
        rig.clock.t = at(hhmm, NEXT)
        await rig.rt.poll_once()
    sent = [e for e in rig.notifier.attempts if e.kind is EventKind.DATA_UNCLEAR]
    assert len(sent) == 2 and sent[1].key.startswith("data_unclear:")  # owner decision 7


def _set_facts(rig: Rig, kind: EventKind, facts: tuple) -> None:
    """Rewrites the held look-ahead message's stored facts (and its kind). A message with an
    adoption or its withdrawal is written without an expiry, as run_lookahead writes it."""
    st = rig.rt._st
    o = next(o for o in st.obligations if o.look_ahead is not None)
    led = st.lookahead["p1|outbound"]
    keeps = any(k.startswith(("adopt:", "cancel:")) for k, *_ in facts)
    ob = replace(o, event=replace(o.event, kind=kind), expires_at=None if keeps else o.expires_at)
    rig.rt._st = replace(
        st,
        obligations=tuple(ob if x.id == o.id else x for x in st.obligations),
        lookahead={
            **st.lookahead,
            "p1|outbound": replace(
                led,
                pending=tuple(ob if x.id == o.id else x for x in led.pending),
                facts={**led.facts, o.id: facts},
            ),
        },
    )


async def test_undelivered_works_warning_is_dropped_after_its_date(tmp_path):
    rig = await _held_warning(tmp_path, DAY + timedelta(days=1))
    await rig.rt.watchdog_once()
    rig.clock.t += timedelta(days=2)
    await _back(rig)
    assert _ahead(rig, EventKind.WORKS_AHEAD) == []
    assert not any(o.event.kind is EventKind.WORKS_AHEAD for o in rig.rt.state.obligations)
    # the drop leaves the ledger too, as any finished look-ahead message does
    assert rig.rt._st.lookahead["p1|outbound"].pending == ()


def _set_expiry(rig: Rig, expires: object) -> None:
    """Stamps the held look-ahead message with another dated journey end."""
    st = rig.rt._st
    o = next(o for o in st.obligations if o.look_ahead is not None)
    ob = replace(o, expires_at=expires)
    led = st.lookahead["p1|outbound"]
    rig.rt._st = replace(
        st,
        obligations=tuple(ob if x.id == o.id else x for x in st.obligations),
        lookahead={
            **st.lookahead,
            "p1|outbound": replace(
                led, pending=tuple(ob if x.id == o.id else x for x in led.pending)
            ),
        },
    )
    rig.rt._save()


@pytest.mark.parametrize("restart", [False, True])
async def test_held_works_warning_survives_a_recovery_written_through_a_rename(tmp_path, restart):
    rig = Rig(tmp_path, "05:40")  # rule 5(c), 22: a held warning keeps the revision it is on
    rig.provider.world = _cancelled_l2(DAY + timedelta(days=1))
    rig.notifier.fails = lambda e: True
    await rig.rt.start()
    rig.store.fail = True
    await rig.rt.lookahead_once()
    rig.store.fail = False
    rig.write("office_c")
    rig.clock.jump(1)
    await rig.rt.check_file()  # storage is back through a name-only edit
    assert rig.store.load().config.revision == 2
    rig.notifier.fails = lambda e: False
    rig.notifier.attempts.clear()
    if restart:
        await rig.restart()
    await rig.rt.watchdog_once()
    assert _ahead(rig, EventKind.WORKS_AHEAD)


async def test_held_works_warning_crossing_midnight_survives_midnight_and_restart(tmp_path):
    # a 23:40 journey whose transfer leaves at 00:05: its works warning outlives its date
    wed = DAY + timedelta(days=1)
    rig = await _held_warning(tmp_path, wed)
    o = next(o for o in rig.rt.state.obligations if o.look_ahead is not None)
    assert o.expires_at is not None  # the dated journey end, stored with the message
    _set_expiry(rig, at("00:40", wed + timedelta(days=1)) + timedelta(minutes=150))
    rig.clock.t = at("00:00", wed + timedelta(days=1))
    await rig.restart()
    await _back(rig)
    assert _ahead(rig, EventKind.WORKS_AHEAD)


async def test_held_works_warning_dropped_once_its_dated_journey_ended(tmp_path):
    wed = DAY + timedelta(days=1)
    rig = await _held_warning(tmp_path, wed)
    _set_expiry(rig, at("00:40", wed + timedelta(days=1)) + timedelta(minutes=150))
    rig.clock.t = at("03:11", wed + timedelta(days=1))
    await rig.restart()
    await _back(rig)
    assert _ahead(rig, EventKind.WORKS_AHEAD) == []
    assert rig.rt._st.lookahead["p1|outbound"].pending == ()


async def _held_changed_journey(tmp_path, day: date, l1: object, l2: object) -> Rig:
    """05:40 the day before `day`: works change the outbound on `day` to `l1` then `l2`."""
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = [
        *(c for c in every_day() if c.legs[0].train.operating_date != day),
        make_connection([l1, l2]),
        *returns_every_day("17:52"),
    ]
    rig.notifier.fails = lambda e: True
    await rig.rt.start()
    await rig.rt.lookahead_once()
    return rig


@pytest.mark.parametrize(
    ("l2", "end"),
    [
        # the first leg runs to 11:58 and the changed onward train at 12:05 is cancelled
        (("12:05", "12:40", "11:58"), "15:10"),
        # a slow replacement on the usual onward train
        (("08:50", "12:40", "08:40"), "15:10"),
    ],
)
async def test_held_warning_outlives_the_baseline_end_when_works_move_the_arrival(
    tmp_path, l2, end
):
    # 7.12 rule 6: the follow limit counts from the affected journey's own last arrival
    wed = DAY + timedelta(days=1)
    dep, arr, l1_arr = l2
    rig = await _held_changed_journey(
        tmp_path,
        wed,
        make_leg("AAA", "UT", "08:12", l1_arr, number="1234", day=wed),
        make_leg("UT", "RTD", dep, arr, number="5678", day=wed, cancelled=True),
    )
    o = next(o for o in rig.rt.state.obligations if o.event.kind is EventKind.WORKS_AHEAD)
    assert o.expires_at == at(end, wed)  # 12:40 plus 150 min, not 09:24 plus 150 min
    rig.clock.t = at("12:00", wed)  # after the baseline end, 09:24 plus 150 min
    await rig.restart()
    await _back(rig)
    assert _ahead(rig, EventKind.WORKS_AHEAD)


async def test_held_works_warning_without_a_known_end_is_kept_the_next_day(tmp_path):
    wed = DAY + timedelta(days=1)
    rig = await _held_warning(tmp_path, wed)
    _set_expiry(rig, None)  # stored before the end was kept, or no baseline: uncertain
    rig.clock.t = at("00:00", wed + timedelta(days=1))
    await rig.restart()
    await _back(rig)
    assert _ahead(rig, EventKind.WORKS_AHEAD)


async def test_held_lookahead_profile_refused_after_its_date_is_still_sent(tmp_path):
    # an app warning is sent before fact_holds runs (rule 5a beats 5c)
    rig = await _held_warning(tmp_path, DAY + timedelta(days=1))
    ((_, fs),) = rig.rt._st.lookahead["p1|outbound"].facts.items()
    _set_facts(rig, EventKind.PROFILE_REFUSED, fs)  # a stale per-date fact: 5(b) would drop it
    rig.clock.t += timedelta(days=2)
    await _back(rig)
    assert _ahead(rig, EventKind.PROFILE_REFUSED)


async def test_undelivered_works_warning_still_goes_out_on_its_own_date(tmp_path):
    tomorrow = DAY + timedelta(days=1)
    rig = await _held_warning(tmp_path, tomorrow)
    rig.clock.t = at("06:30", tomorrow)
    await _back(rig)
    got = _ahead(rig, EventKind.WORKS_AHEAD)
    assert got and "28 Oct" in got[0].text


async def test_bundled_adoption_and_warning_after_its_date_is_still_sent(tmp_path):
    rig = await _held_warning(tmp_path, DAY + timedelta(days=1))
    led = rig.rt._st.lookahead["p1|outbound"]
    ((oid, fs),) = led.facts.items()
    adopt = ("adopt:2", "your usual train is now 08:22", False, False)
    _set_facts(rig, EventKind.WORKS_AHEAD, (adopt, *fs))
    assert any(k.startswith("adopt:") for k, *_ in rig.rt._st.lookahead["p1|outbound"].facts[oid])
    assert any("|warn:" in k for k, *_ in fs)
    rig.clock.t += timedelta(days=2)
    await _back(rig)
    assert _ahead(rig, EventKind.WORKS_AHEAD)


async def test_withdrawn_adoption_correction_after_its_date_is_still_sent(tmp_path):
    rig = await _held_warning(tmp_path, DAY + timedelta(days=1))
    _set_facts(rig, EventKind.RESTORED, (("cancel:2", "ignore the new time", False, False),))
    rig.clock.t += timedelta(days=2)
    await _back(rig)
    assert _ahead(rig, EventKind.RESTORED)


async def test_held_warning_whose_works_ended_is_dropped_restored_still_sent(tmp_path):
    rig = await _held_warning(tmp_path, DAY + timedelta(days=2))
    rig.clock.t = at("05:40", DAY + timedelta(days=1))
    rig.provider.world = _clean()
    await rig.rt.lookahead_once()
    assert any(o.event.kind is EventKind.RESTORED for o in rig.rt.state.obligations)
    await _back(rig)
    got = [e.kind for e in _ahead(rig, EventKind.WORKS_AHEAD) + _ahead(rig, EventKind.RESTORED)]
    assert got == [EventKind.RESTORED]


async def test_held_restoration_followed_by_renewed_works_is_dropped(tmp_path):
    thu = DAY + timedelta(days=2)
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = _cancelled_l2(thu)
    await rig.rt.start()
    await rig.rt.lookahead_once()
    assert _ahead(rig, EventKind.WORKS_AHEAD)  # delivered
    rig.notifier.fails = lambda e: True
    rig.clock.t = at("05:40", DAY + timedelta(days=1))
    rig.provider.world = _clean()
    await rig.rt.lookahead_once()
    assert any(o.event.kind is EventKind.RESTORED for o in rig.rt.state.obligations)
    rig.clock.set("12:00")
    rig.provider.world = _cancelled_l2(thu)
    await rig.rt.lookahead_once()
    await _back(rig)
    got = [e.kind for e in _ahead(rig, EventKind.WORKS_AHEAD) + _ahead(rig, EventKind.RESTORED)]
    assert got == [EventKind.WORKS_AHEAD]


async def test_held_whole_journey_warning_still_goes_out(tmp_path):
    tomorrow = DAY + timedelta(days=1)
    rig = Rig(tmp_path, "05:40")
    # leg 2: +4 min departure, +9 min duration: a whole-journey change, no leg broken
    rig.provider.world = _world_with(
        tomorrow, make_leg("UT", "RTD", "08:54", "09:37", number="5678", day=tomorrow)
    )
    rig.notifier.fails = lambda e: True
    await rig.rt.start()
    await rig.rt.lookahead_once()
    fs = [k for v in rig.rt._st.lookahead["p1|outbound"].facts.values() for k, *_ in v]
    assert fs and all("|j|warn:" in k and ";" in k for k in fs)
    rig.clock.t = at("06:30", tomorrow)
    await _back(rig)
    assert _ahead(rig, EventKind.WORKS_AHEAD)


async def test_held_lookahead_message_after_a_rename_names_the_new_profile(tmp_path):
    rig = await _held_warning(tmp_path, DAY + timedelta(days=1))
    rig.write(text=PROFILE.replace('"NAME"', '"office_b"').replace(
        'name: "office_b"\n    days: [sat]', 'name: "office_c"\n    days: [sat]'))  # fmt: skip
    await rig.rt.reload()
    await _back(rig)
    got = _ahead(rig, EventKind.WORKS_AHEAD)
    assert got and got[0].text.startswith("Office B, ")


async def _cancel_mid_pass(rig: Rig) -> None:
    """A delivery pass whose `key_rejected` send hangs, cancelled there as `docker stop` does;
    every other pending message is sent before it."""
    rt = rig.rt
    rt._app_warning(EventKind.KEY_REJECTED, "your NS key was rejected.", rig.clock.now())
    inner, entered = rig.notifier.send, asyncio.Event()

    async def send(e: AlertEvent) -> bool:
        if e.kind is EventKind.KEY_REJECTED:
            entered.set()
            await asyncio.Event().wait()
        return await inner(e)

    rig.notifier.send = send  # type: ignore[method-assign]
    rig.notifier.fails = lambda e: False
    task = asyncio.create_task(rt._deliver())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rig.notifier.send = inner  # type: ignore[method-assign]


async def test_shutdown_mid_pass_keeps_sent_alerts_and_advice_told(tmp_path):
    rig = Rig(tmp_path, "16:54")  # Review Focus 5, rule 22, 7.6
    rig.notifier.fails = lambda e: True
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()
    assert [o.event.kind for o in rig.rt.state.obligations] == [EventKind.LEAVE_NOW]
    await _cancel_mid_pass(rig)
    stored = rig.store.load()
    assert [o.event.kind for o in stored.obligations] == [EventKind.KEY_REJECTED]
    assert stored.occurrences["p1|return|2026-10-27"].advice.delivered
    n = len(rig.notifier.attempts)
    await rig.restart()
    assert EventKind.LEAVE_NOW not in kinds(rig.notifier.attempts[n:])


async def test_shutdown_mid_pass_keeps_a_sent_lookahead_message_told(tmp_path):
    rig = Rig(tmp_path, "05:40")  # Review Focus 5, 7.10
    rig.provider.world = every_day()
    await rig.rt.start()
    await rig.rt.lookahead_once()
    rig.clock.t = at("05:40", DAY + timedelta(days=1))
    await rig.rt.lookahead_once()
    rig.provider.world = every_day("08:20")
    rig.clock.t = at("05:40", DAY + timedelta(days=2))
    rig.notifier.fails = lambda e: True
    await rig.rt.lookahead_once()
    assert rig.store.load().lookahead["p1|outbound"].pending
    await _cancel_mid_pass(rig)
    stored = rig.store.load()
    assert not stored.lookahead["p1|outbound"].pending
    assert [o.event.kind for o in stored.obligations] == [EventKind.KEY_REJECTED]
    n = len(rig.notifier.attempts)
    await rig.restart()
    assert EventKind.TIMETABLE_ADOPTED not in kinds(rig.notifier.attempts[n:])


def _strip_delivered(text: str) -> str:
    """The stored text as an older release wrote it: advice records without `delivered`."""

    def walk(v):
        if isinstance(v, dict):
            v.pop("delivered", None) if "advised_train" in v else None
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    raw = json.loads(text)
    walk(raw)
    return json.dumps(raw)


async def test_legacy_advice_withdrawn_after_an_upgrade_restart(tmp_path):  # Review Focus 10
    rig = Rig(tmp_path, "16:54")
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()  # LEAVE_NOW sent and confirmed
    assert rig.occ("p1|return|2026-10-27").advice.delivered
    rig.store.text = _strip_delivered(rig.store.text)
    rt = await rig.restart()
    assert rig.occ("p1|return|2026-10-27").advice.delivered
    rig.clock.set("16:56")
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952")]),
        *escape_world(),
    ]
    await rt.poll_once()
    got = kinds(rig.notifier.attempts)
    assert got.count(EventKind.EARLIER_TRAIN_WITHDRAWN) == 1
    assert rig.occ("p1|return|2026-10-27").advice.withdrawn


async def test_legacy_advice_still_owed_is_not_withdrawn_after_an_upgrade_restart(
    tmp_path,
):  # Review Focus 10
    rig = Rig(tmp_path, "16:54")
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    rig.provider.world = cancelled_usual_world()
    await rig.rt.start()
    assert any(o.event.kind is EventKind.LEAVE_NOW for o in rig.rt.state.obligations)
    rig.store.text = _strip_delivered(rig.store.text)
    rt = await rig.restart()
    assert not rig.occ("p1|return|2026-10-27").advice.delivered
    rig.clock.set("16:56")
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952")]),
        *escape_world(),
    ]
    await rt.poll_once()
    rig.notifier.fails = lambda e: False
    await rt.watchdog_once()
    assert EventKind.EARLIER_TRAIN_WITHDRAWN not in kinds(rig.notifier.attempts)
    assert not any(o.event.kind is EventKind.LEAVE_NOW for o in rig.rt.state.obligations)
    assert rig.occ("p1|return|2026-10-27").advice.withdrawn


# --- Review Focus 7: two profiles on one train keep separate alerts (7.12 rules 5, 6, 22) ---


async def two_on_one_train(tmp_path) -> Rig:
    """p1 removed while its journey runs (rule 10) and p2, now on Tuesdays, both follow the 08:12
    from AAA; one poll cancels it for both while the phone is down."""
    rig = await started(tmp_path)
    p2_only = PROFILE.split('  - id: "p2"')[1].replace("days: [sat]", "days: [tue]")
    rig.write(text='profiles:\n  - id: "p2"' + p2_only)
    rig.clock.jump(1)
    await rig.rt.check_file()
    rig.clock.set("07:52")
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: True
    rig.clock.set("07:54")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    return rig


def phone(rig: Rig, took: list[AlertEvent], down_for: str | None = None) -> None:
    """The phone takes every alert but `down_for`'s, and adds the ones it took to `took`."""

    def fails(e: AlertEvent) -> bool:
        if e.profile_id == down_for:
            return True
        took.append(e)
        return False

    rig.notifier.fails = fails


def cancelled_owed(rig: Rig) -> list[Obligation]:
    return [o for o in rig.rt.state.obligations if o.event.kind is EventKind.CANCELLED]


def cancelled_by(took: list[AlertEvent]) -> list[str]:
    return sorted(e.profile_id for e in took if e.kind is EventKind.CANCELLED)


def restore_ids(rig: Rig, ids) -> None:
    """Rewrites the stored obligation ids, through the real codec."""
    st = loads(PersistedState, rig.store.text)
    rig.store.text = dumps(replace(st, obligations=tuple(
        replace(o, id=ids(o)) for o in st.obligations)))  # fmt: skip


async def test_two_profiles_on_one_train_keep_separate_alerts(tmp_path):  # Review Focus 7
    rig = await two_on_one_train(tmp_path)
    owed = cancelled_owed(rig)
    assert sorted(o.event.profile_id for o in owed) == ["p1", "p2"]
    assert len({o.id for o in owed}) == 2
    took: list[AlertEvent] = []
    phone(rig, took, down_for="p2")
    await rig.rt.watchdog_once()
    assert rig.rt.view().undelivered == 1
    assert [o.event.profile_id for o in cancelled_owed(rig)] == ["p2"]
    phone(rig, took)
    await rig.rt.watchdog_once()
    assert cancelled_by(took) == ["p1", "p2"] and not cancelled_owed(rig)


async def test_stored_obligation_ids_load_unchanged(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: True
    rig.clock.set("07:54")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    (o,) = cancelled_owed(rig)
    old = f"{o.event.key}|{o.event.severity.value}|{o.event.created_at.isoformat()}"
    restore_ids(rig, lambda x: old if x.id == o.id else x.id)
    await rig.restart()
    assert [x.id for x in cancelled_owed(rig)] == [old]
    took: list[AlertEvent] = []
    phone(rig, took)
    await rig.rt.watchdog_once()
    assert cancelled_by(took) == ["p1"]
    assert not any(x.id == old for x in rig.store.load().obligations)


async def test_colliding_stored_ids_are_split_on_load(tmp_path):
    rig = await two_on_one_train(tmp_path)
    (o, _) = cancelled_owed(rig)
    old = f"{o.event.key}|{o.event.severity.value}|{o.event.created_at.isoformat()}"
    restore_ids(rig, lambda x: old if x.event.kind is EventKind.CANCELLED else x.id)
    await rig.restart()
    owed = cancelled_owed(rig)
    assert sorted(x.event.profile_id for x in owed) == ["p1", "p2"]
    assert len({x.id for x in owed}) == 2
    took: list[AlertEvent] = []
    phone(rig, took, down_for="p2")
    await rig.rt.watchdog_once()
    assert rig.rt.view().undelivered == 1
    rig.notifier.fails = lambda e: True
    await rig.restart()
    assert [x.event.profile_id for x in cancelled_owed(rig)] == ["p2"]
    phone(rig, took)
    await rig.rt.watchdog_once()
    assert cancelled_by(took) == ["p1", "p2"] and not cancelled_owed(rig)


# --- rule 5(b): a look-ahead bundle sends only its facts that still hold (Review Focus 12) ---


def thu_world(leg0: bool, leg1: bool) -> list:
    """Every usual journey, the outbound two days after DAY with leg 0 and leg 1 cancelled or
    not."""
    thu = DAY + timedelta(days=2)
    l1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=thu, cancelled=leg0)
    l2 = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=thu, cancelled=leg1)
    return [
        *(c for c in every_day() if c.legs[0].train.operating_date != thu),
        make_connection([l1, l2]),
        *returns_every_day("17:52"),
    ]


async def live_facts_bundle(rig: Rig, change: str, stale: bool, fail) -> tuple[Obligation, tuple]:
    """Look-ahead runs on DAY at 05:40, 06:10 (and 06:40) on a rig not started yet; `fail(True)`
    makes every delivery fail from the bundle under test on. "recovery": works on both legs,
    then leg 0 is restored (both when `stale`). "relapse": works on both legs, delivered, both
    back to normal, then leg 1 under works again (both when `stale`). Returns the bundle under
    test, still owed, and the facts of it that still hold."""
    thu = DAY + timedelta(days=2)

    def owed() -> Obligation:
        (o,) = [x for x in rig.rt.state.obligations if x.look_ahead == ("p1", OUT, thu)]
        return o

    rig.provider.world = thu_world(True, True)
    fail(change == "recovery")
    await rig.rt.start()
    await rig.rt.lookahead_once()
    if change == "recovery":
        o = owed()
        rig.clock.set("06:10")
        rig.provider.world = thu_world(False, not stale)
        await rig.rt.lookahead_once()
        keep = f"{thu.isoformat()}|1|warn:"
    else:
        rig.clock.set("06:10")
        rig.provider.world = thu_world(False, False)
        fail(True)
        await rig.rt.lookahead_once()
        o = owed()
        rig.clock.set("06:40")
        rig.provider.world = thu_world(stale, True)
        await rig.rt.lookahead_once()
        keep = f"{thu.isoformat()}|0|restored"
    facts = rig.rt.state.lookahead["p1|outbound"].facts[o.id]
    assert len(facts) > 1  # a bundle
    return o, () if stale else tuple(f for f in facts if f[0].startswith(keep))


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("change", ["relapse", "recovery"])
async def test_partial_relapse_or_recovery_phone_sends_only_live_facts(tmp_path, change, stale):
    rig = Rig(tmp_path, "05:40")

    def fail(down: bool) -> None:
        rig.notifier.fails = lambda e: down

    o, live = await live_facts_bundle(rig, change, stale, fail)
    fail(False)
    rig.notifier.attempts.clear()
    await rig.rt.watchdog_once()
    got = [(e.kind, e.text) for e in rig.notifier.attempts if e.key == o.event.key]
    if stale:
        assert got == []
        return
    p = rig.rt.config.profiles[0]
    assert got == [render_message(p, o.look_ahead[2], live)]
    assert "back to normal" not in got[0][1] if change == "recovery" else "works" not in got[0][1]

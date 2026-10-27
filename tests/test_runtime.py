import asyncio
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from builders import AMS, ONE_TRANSFER, make_connection, make_leg, only_occurrence
from test_lifecycle_follow import L1, L1X, L2, L2X, LATER, conn, journey, occ

from train_commute_companion.lifecycle import (
    Choose,
    DirectionState,
    Leaving,
    LegState,
    LockSource,
    NotTravelling,
)
from train_commute_companion.location import LocationEvent
from train_commute_companion.lookahead import BaselineHistory
from train_commute_companion.model import (
    AlertEvent,
    Connection,
    Direction,
    EventKind,
    OccurrenceKey,
    Provenance,
    Severity,
)
from train_commute_companion.providers.base import (
    AuthError,
    PlannedWorks,
    ProviderAnswer,
    ProviderError,
    Query,
    RateLimited,
    ServerError,
)
from train_commute_companion.runtime import RequestBudget, Runtime, _fresh_state
from train_commute_companion.serde import dumps, loads
from train_commute_companion.store import PersistedState, StorageError

DAY = date(2026, 10, 27)  # a Tuesday
PROFILE = """
profiles:
  - id: "p1"
    name: "NAME"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "RTD"
    transfer_min: {UT: 4}
    outbound: {first_train: "08:12", buffer_min: 10, transfers: [UT]}
    return: {first_train: "17:52", buffer_min: 5}
  - id: "p2"
    name: "office_b"
    days: [sat]
    home_station: "AAA"
    work_station: "RTD"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()} {hhmm}").replace(tzinfo=AMS)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t = (self.t.astimezone(UTC) + timedelta(seconds=seconds)).astimezone(AMS)

    def jump(self, minutes: float) -> None:
        self.t = (self.t.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(AMS)

    def set(self, hhmm: str) -> None:
        self.t = at(hhmm, self.t.date())


class MemoryStore:
    """Keeps the encoded text, so what survives a restart went through the real codec."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.fail = False
        self.writes = 0

    def load(self) -> PersistedState | None:
        return None if self.text is None else loads(PersistedState, self.text)

    def save(self, s: PersistedState) -> None:
        if self.fail:
            raise StorageError("disk full")
        self.text = dumps(s)
        self.writes += 1


class Notifier:
    def __init__(self) -> None:
        self.fails: Callable[[AlertEvent], bool] = lambda e: False
        self.attempts: list[AlertEvent] = []

    async def send(self, event: AlertEvent) -> bool:
        self.attempts.append(event)
        return not self.fails(event)


def pieces(c: Connection, origin: str, destination: str) -> list[Connection]:
    """Every run of whole legs of `c` from `origin` to `destination`."""
    n = len(c.legs)
    return [
        Connection(c.legs[i:j], c.status, None)
        for i in range(n)
        for j in range(i + 1, n + 1)
        if c.legs[i].board.code == origin and c.legs[j - 1].alight.code == destination
    ]


class Provider:
    """Replays a scripted world: every query gets the connections from its origin, fetched now.
    `hook` runs inside a request (to move the clock, act or edit mid-flight); `error` raises.
    `window`: like NS, only connections leaving from the asked time up to that much later."""

    def __init__(self, clock: FakeClock, world: list[Connection]) -> None:
        self.clock = clock
        self.world = world
        self.calls: list[Query] = []
        self.error: Callable[[Query], Exception | None] = lambda q: None
        self.hook: Callable[[Query], object] | None = None
        self.works_calls = 0
        self.window: timedelta | None = None

    async def trips(self, q: Query) -> ProviderAnswer:
        self.calls.append(q)
        if self.hook is not None:
            r = self.hook(q)
            if hasattr(r, "__await__"):
                await r  # type: ignore[misc]
        err = self.error(q)
        if err is not None:
            raise err
        conns = tuple(x for c in self.world for x in pieces(c, q.origin, q.destination))
        if self.window is not None:
            t, w = q.at.astimezone(UTC), self.window
            conns = tuple(c for c in conns if t <= c.legs[0].train.planned_departure < t + w)
        return ProviderAnswer(q, conns, self.clock.now())

    async def planned_works(self) -> PlannedWorks:
        self.works_calls += 1
        return PlannedWorks((), "complete")

    async def stations(self) -> frozenset[str]:
        return frozenset()


USUAL = make_connection([L1, L2])


def usual_on(day: date, first: str = "08:12") -> Connection:
    return make_connection(
        [
            make_leg("AAA", "UT", first, "08:40", number="1234", day=day),
            make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=day),
        ]
    )


def every_day(first: str = "08:12") -> list[Connection]:
    return [usual_on(DAY + timedelta(days=i), first) for i in range(12)]


class Rig:
    def __init__(self, tmp: Path, now: str = "07:50", name: str = "office_a") -> None:
        self.path = tmp / "profiles.yaml"
        self.write(name)
        self.clock = FakeClock(at(now))
        self.store = MemoryStore()
        self.notifier = Notifier()
        self.provider = Provider(self.clock, [USUAL])
        self.rt = self.new()

    def write(self, name: str = "office_a", text: str | None = None) -> None:
        self.path.write_text(text if text is not None else PROFILE.replace("NAME", name))

    def new(self) -> Runtime:
        return Runtime(
            profile_path=self.path,
            store=self.store,
            provider=self.provider,
            notifier=self.notifier,
            clock=self.clock,
            known_stations=None,
        )

    async def restart(self) -> Runtime:
        self.rt = self.new()
        await self.rt.start()
        return self.rt

    def occ(self, key: str = "p1|outbound|2026-10-27"):
        return self.rt.state.occurrences.get(key)

    def occ_set(self, s, key: str = "p1|outbound|2026-10-27") -> None:
        st = self.rt._st
        self.rt._st = replace(st, occurrences={**st.occurrences, key: s})

    async def run_until(self, hhmm: str, poll: bool = True) -> None:
        """Polls every 2 min and runs the watchdog every 30 s up to `hhmm`."""
        end = at(hhmm, self.clock.t.date())
        while self.clock.t < end:
            if poll and self.clock.t.minute % 2 == 0 and self.clock.t.second == 0:
                await self.rt.poll_once()
            await self.rt.watchdog_once()
            await self.clock.sleep(30)


def kinds(events: list[AlertEvent]) -> list[EventKind]:
    return [e.kind for e in events]


async def started(tmp: Path, now: str = "07:50") -> Rig:
    rig = Rig(tmp, now)
    await rig.rt.start()
    await rig.rt.poll_once()
    return rig


# --- request budget ---


def test_backoff_steps_cap_and_reset():
    now = at("08:00")
    b = RequestBudget()
    steps = []
    for _ in range(4):
        b = b.on_result(RateLimited(), now, None)
        steps.append((b.cooldown_until - now.astimezone(UTC)) // timedelta(minutes=1))
    assert steps == [2, 4, 8, 8]
    capped = RequestBudget().on_result(ServerError(), now, at("08:01"))
    assert capped.cooldown_until == at("08:01").astimezone(UTC)
    assert not capped.allow(now, live=True)
    assert capped.allow(at("08:01"), live=True)
    assert not capped.allow(at("08:01"), live=False)  # live journeys first (rule 14)
    assert b.on_result(None, now, None) == RequestBudget()
    assert RequestBudget().on_result(AuthError(), now, None).reason == "auth"


def test_auth_backoff_steps_cap_reset_and_family_change():
    now, b, steps = at("08:00"), RequestBudget(), []
    for _ in range(5):
        b = b.on_result(AuthError(), now, None)
        steps.append((b.cooldown_until - now.astimezone(UTC)) // timedelta(minutes=1))
    assert steps == [2, 5, 15, 30, 30] and b.reason == "auth"
    r = b.on_result(RateLimited(), now, None)
    assert (r.reason, r.backoff_step, r.cooldown_until - now.astimezone(UTC)) == (
        "rate",
        1,
        timedelta(minutes=2),
    )
    assert b.on_result(None, now, None) == RequestBudget()


async def test_429_backoff_shared_across_profiles(tmp_path):
    rig = await started(tmp_path)
    rig.provider.error = lambda q: RateLimited()
    rig.clock.set("07:52")
    n = len(rig.provider.calls)
    await rig.rt.poll_once()
    assert len(rig.provider.calls) == n + 1  # the first refusal stops the poll
    rig.provider.error = lambda q: None
    await rig.rt.lookahead_once()  # look-ahead for both profiles waits on the same cooldown
    assert len(rig.provider.calls) == n + 1 and rig.provider.works_calls == 0
    rig.clock.set("07:53")
    await rig.rt.poll_once()
    assert len(rig.provider.calls) == n + 1  # still cooling down
    rig.clock.set("07:54")
    await rig.rt.poll_once()
    assert len(rig.provider.calls) > n + 1


# --- polling ---


async def test_poll_deadline_drops_late_answer(tmp_path):
    rig = await started(tmp_path)
    assert rig.occ().lock is not None
    records = rig.occ().poll_records
    rig.clock.set("08:00")
    # the answer arrives 91 s after it was asked for and shows the usual first train cancelled
    rig.provider.world = [make_connection([L1X, L2])]
    rig.provider.hook = lambda q: rig.clock.jump(91 / 60)
    await rig.rt.poll_once()
    assert rig.occ().poll_records[-1].error == "timeout"
    assert len(rig.occ().poll_records) == len(records) + 1
    assert EventKind.CANCELLED not in kinds(rig.rt.events_log)


async def test_poll_started_before_choose_is_discarded(tmp_path):
    rig = await started(tmp_path)
    gen = rig.occ().generation
    records = rig.occ().poll_records
    picked = ProviderAnswer(Query("AAA", "RTD", at("07:50")), (LATER,), at("07:50"))

    async def choose_mid_flight(q: Query) -> None:
        rig.provider.hook = None
        await rig.rt.act("p1", Choose(None, LATER, (picked,)))

    rig.clock.set("07:52")
    rig.provider.world = [USUAL]  # the stale answer lacks the chosen trains
    rig.provider.hook = choose_mid_flight
    await rig.rt.poll_once()
    s = rig.occ()
    assert s.lock.legs[0].train == LATER.legs[0].train
    assert s.generation > gen
    assert s.poll_records == records  # the stale poll was not applied
    assert EventKind.MISSING not in kinds(rig.rt.events_log)


async def test_all_good_flag_set_on_first_lock_even_when_not_proven(tmp_path):
    rig = Rig(tmp_path)
    rig.provider.world = [make_connection([make_leg("AAA", "UT", "08:12", "08:40",
                          number="1234", provenance=Provenance.PLANNED), L2])]  # fmt: skip
    await rig.rt.start()
    await rig.rt.poll_once()
    s = rig.occ()
    assert s.lock is not None and s.all_good_evaluated
    assert EventKind.ALL_GOOD not in kinds(rig.rt.events_log)
    rig.provider.world = [USUAL]  # data turning good later sends no all_good
    rig.clock.set("07:52")
    await rig.rt.poll_once()
    assert EventKind.ALL_GOOD not in kinds(rig.rt.events_log)


async def test_all_good_sent_once_on_a_proven_first_lock(tmp_path):
    rig = await started(tmp_path)
    assert kinds(rig.rt.events_log).count(EventKind.ALL_GOOD) == 1
    rig.clock.set("07:52")
    await rig.rt.poll_once()
    assert kinds(rig.rt.events_log).count(EventKind.ALL_GOOD) == 1


# --- key rejected, watchdog ---


async def test_401_key_rejected_once_per_outage_and_again_each_window(tmp_path):
    rig = Rig(tmp_path, "07:20")
    rig.provider.error = lambda q: AuthError()
    await rig.rt.start()
    await rig.rt.poll_once()
    rig.clock.set("07:22")
    await rig.rt.poll_once()
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 1
    rig.clock.set("16:54")  # the return window opens while the outage lasts
    await rig.rt.poll_once()
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 2
    rig.provider.error = lambda q: None
    rig.clock.set("17:10")  # past the 15-min cooldown the 16:54 rejection started
    await rig.rt.poll_once()
    rig.provider.error = lambda q: AuthError()
    rig.clock.set("17:12")
    await rig.rt.poll_once()  # a new outage
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 3
    assert all(e.severity is Severity.LOUD for e in rig.rt.events_log
               if e.kind is EventKind.KEY_REJECTED)  # fmt: skip


async def test_401_backs_off_and_keeps_key_rejected(tmp_path):
    rig = Rig(tmp_path, "07:20")
    rig.provider.error = lambda q: AuthError()
    times: list[str] = []
    rig.provider.hook = lambda q: times.append(rig.clock.now().strftime("%H:%M"))
    await rig.rt.start()  # the reconcile poll asks once at 07:20
    for _ in range(25):  # polls every 2 min up to 08:10
        await rig.rt.poll_once()
        rig.clock.jump(2)
    assert times == ["07:20", "07:22", "07:28", "07:44"]
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 1
    assert rig.rt.view().occurrences[0].health.auth_failed  # skipped polls still read as auth


async def test_stopped_poller_watchdog_sends_cannot_see_train(tmp_path):
    rig = await started(tmp_path, "07:56")
    assert rig.occ().lock is not None
    await rig.run_until("08:03", poll=False)  # the poller stopped at 07:56
    blind = [e for e in rig.rt.events_log if e.kind is EventKind.CANNOT_SEE_TRAIN]
    assert len(blind) == 1 and blind[0].severity is Severity.LOUD
    await rig.rt.poll_once()  # the poller is back
    await rig.rt.watchdog_once()
    assert kinds(rig.rt.events_log)[-1] is EventKind.CAN_SEE_TRAIN_AGAIN


async def test_unobserved_departure_does_not_declare_recovery(tmp_path):
    rig = await started(tmp_path, "07:56")
    assert rig.occ().lock is not None
    await rig.run_until("08:20", poll=False)  # the poller stopped before the 08:12 departure
    ks = kinds(rig.rt.events_log)
    # no answer arrived after 07:56: the user may be on the 08:12 (rule 7), so no recovery
    assert EventKind.CANNOT_SEE_TRAIN in ks
    assert EventKind.CAN_SEE_TRAIN_AGAIN not in ks


async def test_watchdog_alerts_after_clock_jump(tmp_path):
    rig = await started(tmp_path, "07:52")  # locked, departing in 20 min
    rig.clock.jump(minutes=20)  # no poll ran
    await rig.rt.watchdog_once()
    assert kinds(rig.rt.events_log)[-1] is EventKind.CANNOT_SEE_TRAIN


# --- store ---


async def test_store_write_failure_sends_from_memory_and_storage_failed(tmp_path):
    rig = await started(tmp_path)
    rig.store.fail = True
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    got = kinds(rig.rt.events_log)
    assert EventKind.CANCELLED in got and got.count(EventKind.STORAGE_FAILED) == 1
    rig.clock.set("07:54")
    await rig.rt.poll_once()
    await rig.rt.watchdog_once()
    assert kinds(rig.rt.events_log).count(EventKind.STORAGE_FAILED) == 1  # once per outage
    rig.store.fail = False
    await rig.rt.watchdog_once()  # the retried write lands
    saved = rig.store.load()
    assert saved.occurrences["p1|outbound|2026-10-27"].lock.legs[0].train == LATER.legs[0].train
    rig.store.fail = True
    rig.clock.set("07:56")
    await rig.rt.poll_once()
    assert kinds(rig.rt.events_log).count(EventKind.STORAGE_FAILED) == 2  # a new outage


async def test_failed_poll_write_is_not_active_and_alerts_go_once_from_memory(tmp_path):
    rig = await started(tmp_path)  # rule 22
    written = rig.rt.state
    rig.store.fail = True
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    assert rig.occ().lock == written.occurrences["p1|outbound|2026-10-27"].lock
    assert rig.rt.state.obligations == written.obligations
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 1
    for hhmm in ("07:54", "07:56"):  # recomputed from the written state: not sent again
        rig.clock.set(hhmm)
        await rig.rt.poll_once()
        await rig.rt.watchdog_once()
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 1
    rig.store.fail = False
    await rig.rt.watchdog_once()  # the unwritten change is written and becomes active
    assert rig.occ().lock.legs[0].train == LATER.legs[0].train
    assert rig.store.load().occurrences["p1|outbound|2026-10-27"] == rig.occ()
    rig.clock.set("07:58")
    await rig.rt.poll_once()
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 1


async def test_sent_alert_whose_removal_failed_is_not_sent_again(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    assert any(o.event.kind is EventKind.CANCELLED for o in rig.rt.state.obligations)
    rig.store.fail = True
    rig.notifier.fails = lambda e: False
    for _ in range(3):
        await rig.rt.watchdog_once()
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 2  # one fail, one sent
    rig.store.fail = False
    await rig.rt.watchdog_once()
    assert not any(o.event.kind is EventKind.CANCELLED for o in rig.store.load().obligations)
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 2


async def test_failed_reload_write_keeps_the_last_written_configuration(tmp_path):
    rig = await started(tmp_path)  # 5.2 transactional reload, rule 22
    rig.store.fail = True
    rig.write(
        text=PROFILE.replace("NAME", "office_a").replace(
            '"08:12", buffer_min: 10, transfers', '"09:12", buffer_min: 10, transfers'
        )
    )
    rig.clock.jump(1)
    await rig.rt.check_file()
    assert rig.rt.config.revision == 1 and rig.store.load().config.revision == 1
    assert kinds(rig.notifier.attempts).count(EventKind.STORAGE_FAILED) == 1
    rig.store.fail = False
    await rig.rt.check_file()  # tried again although the file did not change
    assert rig.rt.config.revision == 2 and rig.store.load().config.revision == 2


async def test_failed_lookahead_write_changes_nothing_active(tmp_path):
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = every_day()
    await rig.rt.start()
    before = (rig.rt.state.baselines, rig.rt.state.lookahead)
    rig.store.fail = True
    await rig.rt.lookahead_once()
    assert (rig.rt.state.baselines, rig.rt.state.lookahead) == before
    rig.store.fail = False
    await rig.rt.lookahead_once()
    assert rig.rt.state.lookahead and rig.store.load().lookahead == rig.rt.state.lookahead


async def test_lost_store_runs_fresh_and_sends_storage_failed(tmp_path):
    rig = Rig(tmp_path)

    def lost() -> PersistedState | None:
        raise StorageError("state.json is missing but this app started before: state lost")

    rig.store.load = lost  # type: ignore[method-assign]
    await rig.rt.start()
    assert kinds(rig.rt.events_log)[0] is EventKind.STORAGE_FAILED
    assert rig.occ().lock is not None  # fresh occurrences get the auto lock
    assert rig.store.text is not None  # the write clears the state-lost condition


# --- reload ---


async def test_invalid_edit_keeps_last_config_and_profile_refused(tmp_path):
    rig = await started(tmp_path)
    cfg = rig.rt.config
    rig.write(text="profiles: [\n")
    await rig.rt.check_file()
    assert rig.rt.config is cfg
    assert kinds(rig.rt.events_log)[-1] is EventKind.PROFILE_REFUSED
    assert rig.occ().lock is not None


async def test_missing_file_on_start_runs_stored_config(tmp_path):
    rig = await started(tmp_path)
    cfg = rig.rt.config
    rig.path.unlink()
    rt = await rig.restart()
    assert rt.config == cfg
    assert kinds(rt.events_log) == [EventKind.PROFILE_REFUSED]


async def test_rename_while_riding_keeps_lock(tmp_path):
    rig = await started(tmp_path)
    await rig.run_until("08:20")
    s = rig.occ()
    assert s.direction_state is DirectionState.RIDING
    lock = s.lock
    rig.write("office_x")
    await rig.rt.reload()
    s = rig.occ()
    assert s.lock == lock and s.settings_revision == rig.rt.config.revision
    rig.provider.world = [make_connection([L1, L2X]), LATER]
    rig.clock.set("08:22")
    await rig.rt.poll_once()
    assert rig.rt.events_log[-1].text.startswith("Office X:")


async def test_route_edit_applies_to_the_next_occurrence_not_the_locked_one(tmp_path):
    rig = await started(tmp_path)
    rev = rig.occ().route_revision
    rig.write(text=PROFILE.replace("NAME", "office_a").replace('"17:52"', '"17:40"'))
    await rig.rt.reload()
    s = rig.occ()
    assert s.route_revision == rev and s.lock is not None
    assert rig.rt.config.revision == rev + 1
    assert set(rig.rt.state.config_snapshots) == {rev, rev + 1}


# --- reconcile, restart, archive ---


async def test_restart_reconcile_during_each_arrival_watch(tmp_path):
    rig = await started(tmp_path)
    await rig.run_until("08:56")  # leg 1 accepted and watched, leg 2 not yet arrived
    s = rig.occ()
    assert s.lock.legs[0].state is LegState.DONE and s.lock.legs[0].watch_until is None
    rt = await rig.restart()
    assert rig.occ().lock.legs == s.lock.legs
    await rig.run_until("09:40")  # final leg accepted, its watch runs
    s = rig.occ()
    last = s.lock.legs[-1]
    assert last.state is LegState.DONE and last.watch_until is not None
    rt = await rig.restart()
    assert rig.occ().lock.legs[-1].watch_until == last.watch_until
    await rig.run_until("10:20")
    stopped = [e for e in rt.events_log if e.kind is EventKind.STOPPED_FOLLOWING]
    assert len(stopped) == 1 and stopped[0].severity is Severity.LOUD
    assert rig.occ() is None  # ended, told, window closed: archived


async def test_window_that_closed_while_down_is_resolved_on_start(tmp_path):
    rig = Rig(tmp_path, "07:20")
    rig.provider.world = []
    await rig.rt.start()
    await rig.rt.poll_once()
    assert rig.occ() is not None and rig.occ().lock is None
    rig.clock.set("11:00")
    rt = await rig.restart()
    assert EventKind.STOPPED_FOLLOWING in kinds(rt.events_log)
    assert rig.occ() is None


async def test_occurrence_with_failing_cancelled_delivery_not_archived_across_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.provider.world = [make_connection([L1X, L2])]
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    await rig.rt.start()
    await rig.rt.poll_once()
    assert any(e.kind is EventKind.CANCELLED for e in rig.notifier.attempts)
    rig.clock.set("12:00")  # past the 150-min follow limit and the window
    await rig.rt.watchdog_once()
    assert EventKind.STOPPED_FOLLOWING in kinds(rig.rt.events_log)
    assert rig.occ() is not None
    rt = await rig.restart()
    assert rig.occ() is not None
    rig.notifier.fails = lambda e: False
    await rt.watchdog_once()
    assert EventKind.CANCELLED in kinds(rt.events_log)
    assert rig.occ() is None


async def test_two_occurrences_of_one_direction(tmp_path):
    rig = Rig(tmp_path, "07:50")
    await rig.rt.start()
    # yesterday's occurrence still rides a night train that is not in any answer
    mon = date(2026, 10, 26)
    night = make_leg("AAA", "RTD", "23:50", "03:40", number="9999", day=mon)
    state = rig.rt.state
    ride = replace(journey([night], (LegState.RIDING,)), planned_final_arrival=at("08:40"))
    old = replace(
        occ(ride),
        key=replace(occ(None).key, service_date=mon),
        route_revision=rig.rt.config.revision,
        settings_revision=rig.rt.config.revision,
    )
    rig.store.save(replace(state, occurrences={**state.occurrences, "p1|outbound|2026-10-26": old}))
    rt = await rig.restart()
    await rt.poll_once()
    assert rig.occ("p1|outbound|2026-10-26") is not None
    assert rig.occ().lock is not None  # today's is followed on its own
    missing = [e for e in rt.events_log if e.kind is EventKind.MISSING]
    assert missing and missing[0].service_date == mon
    assert "service date 2026-10-26" in missing[0].text


async def test_not_travelling_drops_failed_cancelled_but_key_rejected_still_goes(tmp_path):
    rig = Rig(tmp_path)
    rig.provider.world = [make_connection([L1X, L2])]
    rig.notifier.fails = lambda e: True
    await rig.rt.start()
    await rig.rt.poll_once()
    rig.provider.error = lambda q: AuthError()
    rig.clock.set("07:52")
    await rig.rt.poll_once()
    await rig.rt.act("p1", NotTravelling(None, False))
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    got = kinds(rig.rt.events_log)
    assert EventKind.KEY_REJECTED in got and EventKind.CANCELLED not in got


# --- look-ahead wiring ---


async def test_lookahead_run_after_route_edit_mid_run_is_discarded_but_next_run_writes(tmp_path):
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = every_day()
    await rig.rt.start()
    edited = PROFILE.replace("NAME", "office_a").replace('"17:52"', '"17:40"')

    async def edit_once(q: Query) -> None:
        rig.provider.hook = None
        rig.write(text=edited)
        await rig.rt.reload()

    rig.provider.hook = edit_once
    await rig.rt.lookahead_once()
    assert rig.rt.config.revision == 2
    assert rig.rt.state.baselines.get("p1|outbound", BaselineHistory()).versions == ()
    rig.clock.set("05:50")
    await rig.rt.lookahead_once()
    assert rig.rt.state.baselines["p1|outbound"].versions != ()
    assert rig.rt.state.lookahead["p1|outbound"].started_revision == 2


async def test_delivered_adoption_leaves_lookahead_pending(tmp_path):
    rig = Rig(tmp_path, "05:40")
    rig.provider.world = every_day()
    await rig.rt.start()
    await rig.rt.lookahead_once()  # seeds the baseline
    rig.clock.t = at("05:40", DAY + timedelta(days=1))
    await rig.rt.lookahead_once()  # confirms it on another commute day
    assert [v.confirmed for v in rig.rt.state.baselines["p1|outbound"].versions] == [True]
    rig.provider.world = every_day("08:20")  # NS moves the usual train
    rig.clock.t = at("05:40", DAY + timedelta(days=2))
    await rig.rt.lookahead_once()
    adopted = [e for e in rig.rt.events_log if e.kind is EventKind.TIMETABLE_ADOPTED]
    assert len(adopted) == 1
    led = rig.rt.state.lookahead["p1|outbound"]
    assert led.pending == ()  # told, so no longer pending
    assert not any(o.event.kind is EventKind.TIMETABLE_ADOPTED for o in rig.rt.state.obligations)
    rig.provider.world = every_day()  # NS takes it back before the date
    rig.clock.t = at("12:00", DAY + timedelta(days=2))
    await rig.rt.lookahead_once()
    restored = [e for e in rig.rt.events_log if e.kind is EventKind.RESTORED]
    assert len(restored) == 1 and "stays as it was" in restored[0].text


async def test_concurrent_delivery_passes_send_each_alert_once(tmp_path):
    rig = Rig(tmp_path)
    rig.provider.world = [make_connection([L1X, L2])]
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    await rig.rt.start()
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: False
    plain = rig.notifier.send

    async def slow(event: AlertEvent) -> bool:
        await asyncio.sleep(0.01)  # a slow Telegram call: the other pass starts meanwhile
        return await plain(event)

    rig.notifier.send = slow  # type: ignore[method-assign]
    before = kinds(rig.notifier.attempts).count(EventKind.CANCELLED)
    await asyncio.gather(rig.rt.watchdog_once(), rig.rt.watchdog_once(), rig.rt.check_file())
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == before + 1


async def test_held_cancellation_names_the_current_lock_when_sent(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.clock.set("07:52")
    later_x = conn(
        make_leg("AAA", "UT", "08:25", "08:53", number="1300", cancelled=True), LATER.legs[1]
    )
    last = conn(
        make_leg("AAA", "UT", "08:40", "09:08", number="1400"),
        make_leg("UT", "RTD", "09:20", "09:54", number="7777"),
    )
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    assert "Next good connection: 08:25." in rig.notifier.attempts[-1].text
    rig.clock.set("07:54")  # the replacement is cancelled too: the lock moves on to 08:40
    rig.provider.world = [make_connection([L1X, L2]), later_x, last]
    await rig.rt.poll_once()
    assert rig.occ().lock.legs[0].train == last.legs[0].train
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    told = [e.text for e in rig.rt.events_log if e.kind is EventKind.CANCELLED]
    assert told and all(
        "08:25." not in t and t.endswith("Next good connection: 08:40.") for t in told
    )


async def test_held_warning_names_the_journey_followed_once_the_locks_first_leg_is_past(tmp_path):
    rig = await started(tmp_path)
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.clock.set("07:52")
    later_x = conn(
        make_leg("AAA", "UT", "08:25", "08:53", number="1300", cancelled=True), LATER.legs[1]
    )
    last = conn(
        make_leg("AAA", "UT", "08:40", "09:08", number="1400"),
        make_leg("UT", "RTD", "09:20", "09:54", number="7777"),
    )
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    rig.clock.set("07:54")
    rig.provider.world = [make_connection([L1X, L2]), later_x, last]
    await rig.rt.poll_once()
    held = [o.event.text for o in rig.rt.state.obligations if o.event.kind is EventKind.CANCELLED]
    # the lock's first leg is past: its second leg (09:20) must not become the "connection"
    s = rig.occ()
    first = replace(s.lock.legs[0], state=LegState.DONE)
    rig.occ_set(replace(s, lock=replace(s.lock, legs=(first, *s.lock.legs[1:]))))
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    told = [e.text for e in rig.rt.events_log if e.kind is EventKind.CANCELLED]
    assert told and all("connection: 08:25" not in t and "connection: 09:20" not in t for t in told)
    # the obsolete 08:25 replacement is not named; the train now followed and its onward leg
    # are, while the warning that already named the whole 08:40 journey stays as written
    assert sorted(t.rsplit(". ", 1)[-1] for t in told) == [
        "Next good connection: 08:40.",
        "Now following your 08:40 train, then the 09:20 from UT.",
    ]
    assert len(held) == 2


# --- shell seams: refusal, flags, started, look-ahead time ---

OVERLAP = PROFILE.replace("days: [sat]", "days: [tue]")


async def test_refusal_set_on_bad_file_and_cleared_on_fix(tmp_path):
    rig = await started(tmp_path)
    assert rig.rt.refusal is None
    rig.write(text=OVERLAP.replace("NAME", "office_a"))
    await rig.rt.check_file()
    assert rig.rt.refusal is not None
    assert "office_a" in rig.rt.refusal and "office_b" in rig.rt.refusal
    rig.write("office_c")
    await rig.rt.check_file()
    assert rig.rt.refusal is None


async def test_refusal_names_the_station_list_wait(tmp_path):
    from train_commute_companion.providers.base import ProviderError

    rig = await started(tmp_path)
    rt = rig.new()
    rt.require_stations = True

    async def down() -> frozenset[str]:
        raise ProviderError("station endpoint down")

    rig.provider.stations = down  # type: ignore[method-assign]
    await rt.start()
    rig.write("office_x")
    await rt.check_file()
    assert rt.refusal is not None and "station list is unavailable" in rt.refusal


async def test_station_list_down_at_restart_is_retried_while_the_file_is_unchanged(tmp_path):
    from train_commute_companion.providers.base import ProviderError

    rig = await started(tmp_path)  # a stored config: it runs, so nothing is waiting (5.2)
    rt = rig.new()
    rt.require_stations = True
    calls: list[bool] = []
    got: list[frozenset[str]] = []
    rt.on_stations = got.append

    async def stations() -> frozenset[str]:
        calls.append(True)
        if len(calls) == 1:
            raise ProviderError("station endpoint down")
        return frozenset({"AAA", "UT", "RTD"})

    rig.provider.stations = stations  # type: ignore[method-assign]
    await rt.start()
    assert rt.known_stations is None and len(calls) == 1
    await rt.check_file()  # not due yet: at most once per poll interval
    assert len(calls) == 1
    rig.clock.jump(2)
    await rt.check_file()  # station zones come from this list (8, 7.11)
    assert rt.known_stations == frozenset({"AAA", "UT", "RTD"}) and got == [rt.known_stations]
    rig.clock.jump(2)
    await rt.check_file()
    assert len(calls) == 2 and rt.refusal is None


async def test_first_start_with_one_bad_profile_and_no_station_list_applies_nothing(tmp_path):
    rig = Rig(tmp_path)
    head, tail = PROFILE.replace("NAME", "office_a").rsplit("buffer_min: 5}", 1)
    rig.write(text=head + "buffer_min: -5}" + tail)  # p2 is invalid, p1 is valid
    rt = rig.rt
    rt.require_stations = True
    up = False

    async def stations() -> frozenset[str]:
        if not up:
            raise ProviderError("station endpoint down")
        return frozenset({"AAA", "UT", "RTD"})

    rig.provider.stations = stations  # type: ignore[method-assign]
    await rt.start()
    assert rt.config is None
    refused = [e for e in rt.events_log if e.kind is EventKind.PROFILE_REFUSED]
    assert len(refused) == 1
    text = refused[0].text
    assert text.index("profiles[1]") < text.index("station list is unavailable")
    rig.clock.jump(1)
    await rt.check_file()  # still waiting: told once per wait
    assert rt.config is None and kinds(rt.events_log).count(EventKind.PROFILE_REFUSED) == 1
    up = True
    rig.clock.jump(2)
    await rt.check_file()
    assert rt.known_stations == frozenset({"AAA", "UT", "RTD"})
    assert rt.config is not None and [p.id for p in rt.config.profiles] == ["p1"]
    assert rt.refusal is not None and "profiles[1]" in rt.refusal
    assert "station list" not in rt.refusal


def _nine_twelve_on(day: date) -> Connection:
    return make_connection(
        [
            make_leg("AAA", "UT", "09:12", "09:40", number="1236", day=day),
            make_leg("UT", "RTD", "09:50", "10:24", number="5680", day=day),
        ]
    )


async def test_removed_and_readded_profile_starts_from_the_file(tmp_path):
    from test_runtime_rules import sig

    from train_commute_companion.lookahead import BaselineVersion

    rig = Rig(tmp_path, "06:00")
    hist = BaselineHistory((BaselineVersion(1, sig(time(8, 12), time(8, 50)), date(2026, 10, 1),
                            True),))  # fmt: skip
    rig.store.save(PersistedState(None, {}, {"p1|outbound": hist}, {}, {}, (), True))
    rig.provider.world = [USUAL, _nine_twelve_on(DAY)]
    await rig.rt.start()
    assert rig.rt.state.baselines["p1|outbound"].versions == hist.versions
    full = PROFILE.replace("NAME", "office_a")
    rig.write(text='profiles:\n  - id: "p2"\n' + full.split('  - id: "p2"\n')[1])
    await rig.rt.reload()
    assert [p.id for p in rig.rt.config.profiles] == ["p2"]
    rig.write(
        text=full.replace(
            '"08:12", buffer_min: 10, transfers', '"09:12", buffer_min: 10, transfers'
        )
    )
    await rig.rt.reload()
    assert rig.rt.config.revision == 3
    assert "p1|outbound" not in rig.rt.state.baselines
    rig.clock.set("07:50")
    await rig.run_until("09:00")
    s = rig.occ()
    assert s is not None and s.lock is not None
    assert s.lock.legs[0].train.planned_departure == at("09:12")


async def test_store_ok_false_after_failed_write(tmp_path):
    rig = await started(tmp_path)
    assert rig.rt.store_ok is True
    rig.store.fail = True
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    assert rig.rt.store_ok is False


async def test_key_rejected_flag_follows_auth_outage(tmp_path):
    rig = Rig(tmp_path, "07:20")
    rig.provider.error = lambda q: AuthError()
    await rig.rt.start()
    await rig.rt.poll_once()
    assert rig.rt.key_rejected is True
    rig.provider.error = lambda q: None
    rig.clock.set("07:22")
    await rig.rt.poll_once()
    assert rig.rt.key_rejected is False


async def test_station_list_auth_error_sets_key_rejected(tmp_path):
    rig = Rig(tmp_path, "07:20")
    rig.rt.require_stations = True

    async def rejected() -> frozenset[str]:
        raise AuthError()

    rig.provider.stations = rejected  # type: ignore[method-assign]
    await rig.rt.start()
    assert rig.rt.key_rejected is True
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 1
    assert next(e for e in rig.rt.events_log if e.kind is EventKind.KEY_REJECTED).severity is (
        Severity.LOUD
    )

    async def ok() -> frozenset[str]:
        return frozenset({"AAA", "UT", "RTD"})

    rig.provider.stations = ok  # type: ignore[method-assign]
    rig.clock.jump(4)  # the rejection's 2-min cooldown, plus one poll interval off live (rule 14)
    await rig.rt.check_file()  # the wait for the list retries once per poll interval
    assert rig.rt.key_rejected is False


async def test_started_false_until_start_returns(tmp_path):
    rig = Rig(tmp_path)
    seen: list[bool] = []
    rig.provider.hook = lambda q: seen.append(rig.rt.started)
    assert rig.rt.started is False
    await rig.rt.start()
    assert rig.rt.started is True
    assert seen and not any(seen)  # the reconcile poll ran before start returned


async def test_now_is_the_runtime_clock(tmp_path):
    rig = Rig(tmp_path)
    assert rig.rt.now() == at("07:50")


class _Stop(Exception):
    pass


async def test_lookahead_at_moves_the_daily_run(tmp_path):
    from datetime import time

    rig = Rig(tmp_path, "04:00")  # before the default look-ahead time too
    rig.rt = Runtime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        known_stations=None,
        lookahead_at=time(7, 12),
    )
    first_works: list[datetime] = []
    plain = rig.provider.planned_works

    async def works() -> PlannedWorks:
        first_works.append(rig.clock.now())
        return await plain()

    rig.provider.planned_works = works  # type: ignore[method-assign]

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(0)
        if first_works or rig.clock.now() >= at("08:12"):
            raise _Stop
        rig.clock.jump(min(seconds, 30) / 60)

    rig.clock.sleep = sleep  # type: ignore[method-assign]
    try:
        await rig.rt.run_forever()
    except _Stop:
        pass
    assert first_works and at("07:12") <= first_works[0] < at("07:12") + timedelta(minutes=1)


# --- choose a train by its time; act reports a miss (7.13, N3) ---

PROFILES_DIR = Path(__file__).parent / "data" / "profiles"


async def one_transfer(tmp: Path, now: str = "07:50", poll: bool = True) -> Rig:
    rig = Rig(tmp, now)
    rig.provider.world = list(ONE_TRANSFER)
    await rig.rt.start()
    if poll:
        await rig.rt.poll_once()
    return rig


def refused(rt: Runtime) -> bool:
    return EventKind.CHOOSE_REFUSED in kinds(rt.events_log)


async def test_choose_at_locks_named_departure(tmp_path):
    rig = await one_transfer(tmp_path)
    assert await rig.rt.choose_at("p1", time(8, 44)) is None
    lock = only_occurrence(rig.rt).lock
    assert lock.legs[0].train.planned_departure == at("08:44")
    assert lock.source is LockSource.CHOOSE and not refused(rig.rt)


@pytest.mark.parametrize(
    "after", ["located_via_ut_telegram", "located_via_ut_off", "located_via_ut_other"]
)
async def test_location_source_changed_during_request_is_not_written(tmp_path, after):  # 8
    rig = Rig(tmp_path)
    rig.write(text=(PROFILES_DIR / "located_via_ut.yaml").read_text())
    rig.provider.world = list(ONE_TRANSFER)
    await rig.rt.start()
    await rig.rt.poll_once()
    source = rig.rt.view().profiles[0].location
    asked, release = asyncio.Event(), asyncio.Event()
    real = rig.provider.trips

    async def held(q):
        if not asked.is_set():
            asked.set()
            await release.wait()
        return await real(q)

    rig.provider.trips = held
    event = LocationEvent("UT", "enter", rig.clock.now(), rig.clock.now())
    task = asyncio.create_task(rig.rt.location("p1", event, source))
    await asked.wait()
    rig.write(text=(PROFILES_DIR / f"{after}.yaml").read_text())
    await rig.rt.check_file()
    release.set()
    await task
    assert only_occurrence(rig.rt).zone_reports == {}


_MTIME = [2_000_000_000 * 10**9]


def bump_mtime(path: Path) -> None:
    """A strictly increasing mtime, so two writes in one tick are both seen by check_file."""
    _MTIME[0] += 10**9
    os.utime(path, ns=(_MTIME[0], _MTIME[0]))


async def _switch_rig(tmp_path):
    """located_via_ut running with its first NS request held until `release` is set."""
    rig = Rig(tmp_path)
    rig.write(text=(PROFILES_DIR / "located_via_ut.yaml").read_text())
    rig.provider.world = list(ONE_TRANSFER)
    await rig.rt.start()
    await rig.rt.poll_once()
    asked, release = asyncio.Event(), asyncio.Event()
    real = rig.provider.trips

    async def held(q):
        if not asked.is_set():
            asked.set()
            await release.wait()
        return await real(q)

    rig.provider.trips = held
    return rig, asked, release


async def _switch_away_and_back(rig) -> None:
    for name in ("located_via_ut_telegram", "located_via_ut"):  # B, then A again
        rig.write(text=(PROFILES_DIR / f"{name}.yaml").read_text())
        bump_mtime(rig.path)
        await rig.rt.check_file()


async def test_location_source_switched_away_and_back_during_request_is_not_written(tmp_path):
    rig, asked, release = await _switch_rig(tmp_path)
    view = rig.rt.view()
    source, epoch = view.profiles[0].location, view.location_epochs.get("p1", 0)
    event = LocationEvent("UT", "enter", rig.clock.now(), rig.clock.now())
    task = asyncio.create_task(rig.rt.location("p1", event, source, epoch))
    await asked.wait()
    await _switch_away_and_back(rig)
    release.set()
    await task
    assert only_occurrence(rig.rt).zone_reports == {}
    assert rig.rt.view().location_epochs["p1"] == epoch + 2


async def test_location_without_epoch_keeps_the_value_check(tmp_path):
    rig, asked, release = await _switch_rig(tmp_path)
    source = rig.rt.view().profiles[0].location
    event = LocationEvent("UT", "enter", rig.clock.now(), rig.clock.now())
    task = asyncio.create_task(rig.rt.location("p1", event, source))
    await asked.wait()
    await _switch_away_and_back(rig)
    release.set()
    await task
    assert "UT" in only_occurrence(rig.rt).zone_reports  # the epoch is the only new fence


async def test_location_epoch_bumps_only_on_location_changes(tmp_path):
    rig = Rig(tmp_path)
    rig.write(text=(PROFILES_DIR / "located_via_ut.yaml").read_text())
    await rig.rt.start()
    first = rig.rt.view().location_epochs.get("p1", 0)
    steps = [
        ("located_via_ut", 0),  # the same file again: no change
        ("located_via_ut_later", 0),  # a route edit, same location
        ("located_via_ut_off", 1),  # location removed
        ("located_via_ut", 2),  # location back
    ]
    for name, want in steps:
        rig.write(text=(PROFILES_DIR / f"{name}.yaml").read_text())
        bump_mtime(rig.path)
        await rig.rt.check_file()
        assert not refused(rig.rt), name
        assert rig.rt.view().location_epochs.get("p1", 0) == first + want, name


async def test_choose_at_retained_remainder_promotes_ridden_train(tmp_path):  # N3, required
    rig = await one_transfer(tmp_path)
    rig.clock.set("08:18")  # at AAA after the 08:12 left: it may still dwell with the user on it
    await rig.rt.location("p1", LocationEvent("AAA", "report", at("08:18"), at("08:18")))
    rig.clock.set("08:20")
    await rig.rt.poll_once()
    pre = only_occurrence(rig.rt)
    assert pre.lock.legs[0].train.planned_departure == at("08:44")
    assert pre.retained[0].legs[0].train.planned_departure == at("08:12")
    ridden_state = pre.retained[0].legs[0].state
    assert ridden_state is LegState.MISSED
    assert await rig.rt.choose_at("p1", time(8, 12)) is None
    s = only_occurrence(rig.rt)
    assert s.lock.legs[0].train.planned_departure == at("08:12")
    assert s.lock.source is LockSource.CHOOSE
    assert s.lock.legs[0].state is ridden_state  # the ridden prefix keeps its progress
    assert s.direction_state is DirectionState.RIDING and s.retained == ()
    assert not refused(rig.rt)


async def test_choose_at_retained_from_missed_transfer_queries_transfer_station(tmp_path):
    rig = await one_transfer(tmp_path)
    await rig.run_until("08:44")
    rig.clock.set("08:53")  # at UT after the delayed 08:46 left: missed transfer, kept retained
    await rig.rt.location("p1", LocationEvent("UT", "report", at("08:53"), at("08:53")))
    pre = only_occurrence(rig.rt)
    assert pre.retained[0].legs[0].train.planned_departure == at("08:46")
    assert await rig.rt.choose_at("p1", time(8, 46)) is None
    assert rig.provider.calls[-1].origin == "UT"
    s = only_occurrence(rig.rt)
    # the 08:12 into UT stays in the lock, and nothing is left alerting (7.12 rule 7)
    assert [x.train.planned_departure for x in s.lock.legs] == [at("08:12"), at("08:46")]
    assert s.lock.legs[0] == pre.lock.legs[0] and s.retained == ()
    assert s.lock.source is LockSource.CHOOSE and not refused(rig.rt)


def departing_from_asked_time(rig: Rig) -> None:
    """As NS: an answer holds only trips leaving at or after the asked time."""
    real = rig.provider.trips

    async def trips(q: Query) -> ProviderAnswer:
        a = await real(q)
        kept = tuple(c for c in a.connections if c.legs[0].train.planned_departure >= q.at)
        return replace(a, connections=kept)

    rig.provider.trips = trips  # type: ignore[method-assign]


async def test_station_report_finds_missed_first_train_ns_no_longer_lists(tmp_path):  # rule 7
    rig = await one_transfer(tmp_path)
    departing_from_asked_time(rig)
    rig.clock.set("08:18")  # at AAA after the 08:12 left; a query from AAA now omits it
    await rig.rt.location("p1", LocationEvent("AAA", "report", at("08:18"), at("08:18")))
    assert EventKind.MISSED_FIRST_TRAIN in kinds(rig.rt.events_log)
    s = only_occurrence(rig.rt)
    assert s.lock.legs[0].train.planned_departure == at("08:44")
    assert s.retained[0].legs[0].train.planned_departure == at("08:12")


async def test_station_report_finds_missed_transfer_ns_no_longer_lists(tmp_path):  # rule 7
    rig = await one_transfer(tmp_path)
    departing_from_asked_time(rig)
    await rig.run_until("08:44")
    rig.clock.set("08:53")  # at UT after the delayed 08:46 left; a query from AAA now omits it
    await rig.rt.location("p1", LocationEvent("UT", "report", at("08:53"), at("08:53")))
    assert EventKind.MISSED_TRANSFER in kinds(rig.rt.events_log)
    assert only_occurrence(rig.rt).retained[0].legs[0].train.planned_departure == at("08:46")


async def test_choose_at_onward_train_after_done_first_leg_keeps_prefix(tmp_path):
    rig = await one_transfer(tmp_path)
    await rig.run_until("08:56")  # leg 1 accepted at UT
    pre = only_occurrence(rig.rt)
    assert pre.lock.legs[0].state is LegState.DONE
    assert pre.lock.legs[1].train.planned_departure == at("08:46")
    assert await rig.rt.choose_at("p1", time(9, 16)) is None
    assert rig.provider.calls[-1].origin == "UT"
    s = only_occurrence(rig.rt)
    assert [x.train.planned_departure for x in s.lock.legs] == [at("08:12"), at("09:16")]
    assert s.lock.legs[0].state is LegState.DONE and s.lock.source is LockSource.CHOOSE
    assert not refused(rig.rt)


async def test_choose_at_onward_train_fetches_board_when_none_yet(tmp_path):
    rig = await one_transfer(tmp_path)
    await rig.run_until("08:56")
    rig.rt._boards.clear()  # as after a restart: no poll answer holds the incoming leg yet
    n = len(rig.provider.calls)
    assert await rig.rt.choose_at("p1", time(9, 16)) is None
    assert len(rig.provider.calls) > n + 1 and rig.provider.calls[-1].origin == "UT"
    assert only_occurrence(rig.rt).lock.legs[1].train.planned_departure == at("09:16")
    assert not refused(rig.rt)


async def test_choose_at_reasons(tmp_path):
    rig = await one_transfer(tmp_path)
    assert "13:00 is outside the outbound window" in await rig.rt.choose_at("p1", time(13, 0))
    assert "no train at 08:13 from AAA" in await rig.rt.choose_at("p1", time(8, 13))
    reason = await rig.rt.choose_at("p2", time(8, 12))  # p2 runs on saturdays
    assert reason == "no journey of Office B is being followed now"
    assert "is being followed" in await rig.rt.choose_at("nobody", time(8, 12))
    other_day = await rig.rt.choose_at("p1", time(8, 44), service_date=date(2026, 10, 28))
    assert "is being followed" in other_day
    assert "is being followed" in await rig.rt.choose_at(
        "p1", time(8, 44), direction=Direction.RETURN
    )


TIGHT = make_connection(  # the 08:44 again, onward after 2 min at UT (transfer_min 4)
    [
        make_leg("AAA", "UT", "08:44", "09:08", number="1236"),
        make_leg("UT", "RTD", "09:10", "09:34", number="5690"),
    ]
)


async def test_choose_at_skips_earlier_arrival_with_impossible_transfer(tmp_path):  # rule 1
    rig = Rig(tmp_path, "07:50")
    rig.provider.world = [*ONE_TRANSFER, TIGHT]
    await rig.rt.start()
    await rig.rt.poll_once()
    assert await rig.rt.choose_at("p1", time(8, 44)) is None
    lock = only_occurrence(rig.rt).lock
    assert [x.train.planned_departure for x in lock.legs] == [at("08:44"), at("09:16")]
    assert lock.source is LockSource.CHOOSE and not refused(rig.rt)


async def test_choose_at_returns_the_refusal_when_no_connection_passes(tmp_path):
    rig = Rig(tmp_path, "07:50")
    rig.provider.world = [ONE_TRANSFER[0], TIGHT]
    await rig.rt.start()
    await rig.rt.poll_once()
    before = only_occurrence(rig.rt).lock
    reason = await rig.rt.choose_at("p1", time(8, 44))
    assert reason is not None and "choice refused: transfer at UT broken" in reason
    assert refused(rig.rt) and only_occurrence(rig.rt).lock == before


async def test_choose_at_two_trains_same_minute_is_refused_plainly(tmp_path):
    rig = Rig(tmp_path)
    twin = make_connection(
        [
            make_leg("AAA", "UT", "08:44", "09:06", number="1238"),
            make_leg("UT", "RTD", "09:12", "09:46", number="5682"),
        ]
    )
    rig.provider.world = [*ONE_TRANSFER, twin]
    await rig.rt.start()
    await rig.rt.poll_once()
    before = only_occurrence(rig.rt).lock
    # 6.1: two trains in one boarding minute stay uncertain even with the number, so the
    # reason never sends the user to a choice the core would refuse
    for number in (None, "1238"):
        reason = await rig.rt.choose_at("p1", time(8, 44), number)
        assert reason == "two trains leave AAA at 08:44; NS data cannot tell them apart"
    assert not refused(rig.rt)
    assert only_occurrence(rig.rt).lock == before


async def test_choose_at_counts_one_request_and_reports_provider_error(tmp_path):
    rig = await one_transfer(tmp_path)
    n, calls = rig.rt.requests, len(rig.provider.calls)
    assert await rig.rt.choose_at("p1", time(8, 44)) is None
    assert rig.rt.requests == n + 1 and len(rig.provider.calls) == calls + 1
    rig.provider.error = lambda q: ProviderError()
    reason = await rig.rt.choose_at("p1", time(8, 12))
    assert reason == "NS did not answer (provider); try again"
    assert rig.rt.requests == n + 2
    assert only_occurrence(rig.rt).lock.legs[0].train.planned_departure == at("08:44")


async def test_choose_at_waits_on_the_shared_backoff(tmp_path):  # rule 14
    rig = await one_transfer(tmp_path)
    rig.provider.error = lambda q: RateLimited()
    assert "NS did not answer (rate)" in await rig.rt.choose_at("p1", time(8, 44))
    rig.provider.error = lambda q: None
    calls = len(rig.provider.calls)
    assert "NS did not answer (rate)" in await rig.rt.choose_at("p1", time(8, 44))
    assert len(rig.provider.calls) == calls  # the cooldown holds the second request back


async def test_choose_at_target_gone_after_request_returns_reason(tmp_path):
    rig = await one_transfer(tmp_path, "07:20", poll=False)  # before the auto lock: not running
    assert rig.occ() is not None and rig.occ().lock is None

    async def remove_p1(q: Query) -> None:
        rig.provider.hook = None
        rig.write(text=PROFILE.split('  - id: "p1"')[0] + "  - id" + PROFILE.split("  - id")[2])
        await rig.rt.reload()

    rig.provider.hook = remove_p1
    reason = await rig.rt.choose_at("p1", time(8, 44))
    assert "is being followed" in reason
    assert rig.occ() is None and not refused(rig.rt)


async def test_act_returns_false_for_unknown_profile_or_no_target(tmp_path):
    rig = await one_transfer(tmp_path)
    assert await rig.rt.act("nobody", NotTravelling(None, False)) is False
    assert await rig.rt.act("p2", NotTravelling(None, False)) is False  # saturdays only
    assert await rig.rt.act("p1", NotTravelling(date(2026, 10, 28), False)) is False
    assert not rig.occ().not_travelling
    assert await rig.rt.act("p1", NotTravelling(None, False)) is True
    assert rig.occ().not_travelling


async def test_act_reaches_a_removed_profile_that_still_runs(tmp_path):
    rig = await one_transfer(tmp_path)
    await rig.run_until("08:20")
    assert rig.occ().direction_state is DirectionState.RIDING
    rig.write(text=PROFILE.split('  - id: "p1"')[0] + "  - id" + PROFILE.split("  - id")[2])
    await rig.rt.reload()
    assert rig.rt.config.profiles[0].id == "p2" and len(rig.rt.config.profiles) == 1
    assert rig.occ() is not None  # runs to its end (rule 10)
    assert await rig.rt.act("p1", NotTravelling(None, False)) is True
    s = rig.occ()
    assert s.not_travelling and s.direction_state is DirectionState.EXPIRED


async def test_whole_day_uses_the_running_route_day_offset(tmp_path):
    rig = Rig(tmp_path, "17:40")
    rig.write(text=(PROFILES_DIR / "night_return.yaml").read_text())
    rig.provider.world = [make_connection([make_leg("UT", "AAA", "17:52", "18:20", number="2222")])]
    await rig.rt.start()
    await rig.rt.poll_once()
    ret = "p1|return|2026-10-27"
    assert rig.occ(ret).lock is not None  # the return started with day_offset 0
    rig.write(text=(PROFILES_DIR / "night_return_offset.yaml").read_text())
    await rig.rt.reload()
    assert rig.rt.config.profiles[0].return_.day_offset == 1
    assert await rig.rt.act("p1", NotTravelling(None, True)) is True
    assert rig.occ(ret).not_travelling
    # the commute day of the running route: tonight's outbound, not the one of the day before
    assert rig.occ("p1|outbound|2026-10-27").not_travelling


async def test_act_direction_picks_that_direction_on_a_shared_date(tmp_path):
    rig = await one_transfer(tmp_path)
    key = OccurrenceKey("p1", Direction.RETURN, DAY)
    rev = rig.rt.config.revision
    rig.occ_set(_fresh_state(key, rev, None), "p1|return|2026-10-27")
    assert await rig.rt.act("p1", NotTravelling(DAY, False), Direction.RETURN) is True
    assert rig.occ("p1|return|2026-10-27").not_travelling
    assert not rig.occ().not_travelling and rig.occ().lock is not None  # outbound keeps alerting


async def test_choose_at_401_sets_key_rejected(tmp_path):
    rig = await one_transfer(tmp_path)
    rig.provider.error = lambda q: AuthError()
    assert "NS did not answer (auth)" in await rig.rt.choose_at("p1", time(8, 44))
    assert rig.rt.key_rejected
    assert EventKind.KEY_REJECTED in kinds(rig.rt.events_log)


# --- choose_at under concurrency, freshness and the shell's deadline (rule 1, 21; 7.13) ---


async def test_choose_at_slow_answer_never_overwrites_a_newer_choice(tmp_path):  # rule 21
    rig = await one_transfer(tmp_path)

    async def newer_choice(q: Query) -> None:
        rig.provider.hook = None
        assert await rig.rt.choose_at("p1", time(8, 12)) is None

    rig.provider.hook = newer_choice
    reason = await rig.rt.choose_at("p1", time(8, 44))
    assert reason == "your journey changed while NS answered; try again"
    lock = only_occurrence(rig.rt).lock
    assert lock.legs[0].train.planned_departure == at("08:12")
    assert lock.source is LockSource.CHOOSE


async def test_choose_at_onward_change_judged_on_fresh_incoming_data(tmp_path):  # rule 1
    rig = await one_transfer(tmp_path)
    await rig.run_until("08:56")  # leg 1 done, its answer cached by the last poll
    first = ONE_TRANSFER[0]
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=40)  # now 09:20
    rig.provider.world = [make_connection([late, first.legs[1]]), ONE_TRANSFER[1]]
    rig.clock.set("09:10")
    reason = await rig.rt.choose_at("p1", time(9, 16))
    assert reason is not None and "choice refused: transfer at UT broken" in reason
    assert refused(rig.rt)  # the 09:16 leaves before the incoming train now arrives
    assert only_occurrence(rig.rt).lock.legs[1].train.planned_departure == at("08:46")


async def test_choose_at_onward_train_after_the_window_end(tmp_path):
    rig = await one_transfer(tmp_path)
    await rig.run_until("08:56")
    onward = make_connection([make_leg("UT", "RTD", "10:30", "11:04", number="5690")])
    rig.provider.world = [*ONE_TRANSFER, onward]
    assert await rig.rt.choose_at("p1", time(10, 30)) is None
    assert only_occurrence(rig.rt).lock.legs[1].train.planned_departure == at("10:30")
    assert not refused(rig.rt)


async def test_choose_at_shares_one_deadline_across_its_requests(tmp_path):
    rig = await one_transfer(tmp_path)
    await rig.run_until("08:56")  # two board queries (UT and the incoming AAA) plus the choice

    async def slow(q: Query) -> None:
        await rig.clock.sleep(35)

    rig.provider.hook = slow
    reason = await rig.rt.choose_at("p1", time(9, 16))
    assert reason == "NS did not answer (timeout); try again"  # 105 s > the 90 s deadline
    assert only_occurrence(rig.rt).lock.legs[1].train.planned_departure == at("08:46")


async def test_view_profiles_are_detached_from_the_runtime(tmp_path):
    rig = await one_transfer(tmp_path)
    v = rig.rt.view()
    v.profiles[0].transfer_min["UT"] = 99  # type: ignore[index]
    assert rig.rt.config.profiles[0].transfer_min["UT"] == 4
    assert rig.rt.view().profiles[0].transfer_min["UT"] == 4


async def test_dated_choose_or_leaving_refused_on_a_skipped_occurrence(tmp_path):
    rig = await one_transfer(tmp_path)
    assert await rig.rt.act("p1", NotTravelling(None, False)) is True
    before = rig.occ()
    assert await rig.rt.act("p1", Leaving(DAY, rig.clock.now())) is False
    choice = Choose(DAY, ONE_TRANSFER[1], ())
    assert await rig.rt.act("p1", choice) is False
    assert "is being followed" in await rig.rt.choose_at("p1", time(8, 44), service_date=DAY)
    assert rig.occ() == before and not refused(rig.rt)
    assert await rig.rt.act("p1", NotTravelling(DAY, False)) is True  # explicit skip still lands

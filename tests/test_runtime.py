from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from builders import AMS, make_connection, make_leg
from test_lifecycle_follow import L1, L1X, L2, L2X, LATER, journey, occ

from train_commute_companion.lifecycle import Choose, DirectionState, LegState, NotTravelling
from train_commute_companion.lookahead import BaselineHistory
from train_commute_companion.model import AlertEvent, Connection, EventKind, Provenance, Severity
from train_commute_companion.providers.base import (
    AuthError,
    PlannedWorks,
    ProviderAnswer,
    Query,
    RateLimited,
    ServerError,
)
from train_commute_companion.runtime import RequestBudget, Runtime
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
    `hook` runs inside a request (to move the clock, act or edit mid-flight); `error` raises."""

    def __init__(self, clock: FakeClock, world: list[Connection]) -> None:
        self.clock = clock
        self.world = world
        self.calls: list[Query] = []
        self.error: Callable[[Query], Exception | None] = lambda q: None
        self.hook: Callable[[Query], object] | None = None
        self.works_calls = 0

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
    assert RequestBudget().on_result(AuthError(), now, None) == RequestBudget()


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
    rig.clock.set("16:56")
    await rig.rt.poll_once()
    rig.provider.error = lambda q: AuthError()
    rig.clock.set("16:58")
    await rig.rt.poll_once()  # a new outage
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 3
    assert all(e.severity is Severity.LOUD for e in rig.rt.events_log
               if e.kind is EventKind.KEY_REJECTED)  # fmt: skip


async def test_stopped_poller_watchdog_sends_cannot_see_train(tmp_path):
    rig = await started(tmp_path, "07:56")
    assert rig.occ().lock is not None
    await rig.run_until("08:03", poll=False)  # the poller stopped at 07:56
    blind = [e for e in rig.rt.events_log if e.kind is EventKind.CANNOT_SEE_TRAIN]
    assert len(blind) == 1 and blind[0].severity is Severity.LOUD
    await rig.rt.poll_once()  # the poller is back
    await rig.rt.watchdog_once()
    assert kinds(rig.rt.events_log)[-1] is EventKind.CAN_SEE_TRAIN_AGAIN


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

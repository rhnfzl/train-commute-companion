from dataclasses import replace
from datetime import date, time

from builders import ONE_TRANSFER, make_connection, make_leg, only_occurrence
from test_runtime import PROFILE, FakeClock, MemoryStore, Notifier, Provider, at

from train_commute_companion.lifecycle import DirectionState, LegState
from train_commute_companion.lookahead import BaselineHistory, BaselineVersion, LookAheadLedger
from train_commute_companion.model import (
    Direction,
    Provenance,
    Signature,
    SignatureLeg,
    TransferState,
)
from train_commute_companion.profile import load_config
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query
from train_commute_companion.runtime import Runtime
from train_commute_companion.view import Moved, WorksDay, moved, occurrence_view, works_ahead

DIRECT = PROFILE.replace(", transfers: [UT]", "")
WITHOUT_P1 = "profiles:\n" + PROFILE[PROFILE.index('  - id: "p2"') :]


class World:
    def __init__(self, tmp, conns=ONE_TRANSFER, now: str = "07:50") -> None:
        self.path = tmp / "profiles.yaml"
        self.write(PROFILE)
        self.clock = FakeClock(at(now))
        self.notifier = Notifier()
        self.provider = Provider(self.clock, list(conns))
        self.rt = Runtime(
            profile_path=self.path,
            store=MemoryStore(),
            provider=self.provider,
            notifier=self.notifier,
            clock=self.clock,
            known_stations=None,
        )

    def write(self, text: str) -> None:
        self.path.write_text(text.replace("NAME", "office_a"))

    async def start(self) -> "World":
        await self.rt.start()
        await self.rt.poll_once()
        return self

    def set_occ(self, s) -> None:
        st = self.rt._st
        k = next(k for k, v in st.occurrences.items() if v.key == s.key)
        self.rt._st = replace(st, occurrences={**st.occurrences, k: s})

    def view_with(self, conns):
        """The occurrence's view over one answer holding `conns` instead of the polled ones."""
        s = only_occurrence(self.rt)
        q = Query("AAA", "RTD", self.clock.now())
        answer = ProviderAnswer(q, tuple(conns), self.clock.now())
        return occurrence_view(s, self.rt._ctx(s, self.clock.now()), (answer,))


async def locked(tmp, conns=ONE_TRANSFER) -> World:
    w = await World(tmp, conns).start()
    w.clock.set("08:00")
    await w.rt.poll_once()
    assert only_occurrence(w.rt).lock is not None
    return w


async def test_view_shows_locked_legs_delay_and_platform(tmp_path):
    w = await locked(tmp_path)
    o = w.rt.view().occurrences[0]
    assert [x.state for x in o.legs] == ["ok", "delayed"]
    assert (o.legs[1].departure_delay_min, o.legs[1].departure_platform) == (5, "7")
    assert o.legs[1].departure == at("08:51") and o.legs[1].planned_departure == at("08:46")
    assert (o.legs[0].board, o.legs[0].alight, o.legs[0].progress) == (
        "AAA",
        "UT",
        LegState.PENDING,
    )
    assert o.transfers[0].station == "UT" and o.transfers[0].state is TransferState.OK
    assert (o.transfers[0].margin_min, o.transfers[0].minimum) == (11, 4)


async def test_view_arrival_only_delay_is_shown(tmp_path):
    w = await locked(tmp_path)
    first, nxt = ONE_TRANSFER
    late = make_leg("UT", "RTD", "08:46", "09:20", number="5678", delay_arr=5)
    o = w.view_with([make_connection([first.legs[0], late]), nxt])
    assert o.legs[1].state == "delayed"
    assert (o.legs[1].departure_delay_min, o.legs[1].arrival_delay_min) == (0, 5)
    assert o.legs[1].arrival == at("09:25") and o.legs[1].planned_arrival == at("09:20")


async def test_view_leg_absent_is_missing_and_its_transfer_at_risk(tmp_path):
    w = await locked(tmp_path)
    first, nxt = ONE_TRANSFER
    o = w.view_with([make_connection([first.legs[0]]), nxt])
    assert o.legs[1].state == "missing" and o.legs[1].departure is None
    assert o.legs[1].planned_departure == at("08:46")
    t = o.transfers[0]
    assert (t.state, t.margin_min, t.minimum) == (TransferState.AT_RISK, None, 4)


async def test_view_planned_only_time_has_no_departure_and_transfer_at_risk(tmp_path):
    w = await locked(tmp_path)
    first, nxt = ONE_TRANSFER
    planned = make_leg("UT", "RTD", "08:46", "09:20", number="5678", provenance=Provenance.PLANNED)
    o = w.view_with([make_connection([first.legs[0], planned]), nxt])
    assert o.legs[1].state == "ok"
    assert (o.legs[1].departure, o.legs[1].arrival) == (None, None)
    assert o.transfers[0].state is TransferState.AT_RISK


async def test_view_next_good_follows_the_unfinished_leg(tmp_path):
    w = await locked(tmp_path)
    nxt = w.rt.view().occurrences[0].next_good
    assert nxt is not None and nxt.legs[0].train.planned_departure == at("08:44")
    w.clock.set("08:20")  # the 08:12 left, no missed-leg report
    await w.rt.poll_once()
    s = only_occurrence(w.rt)
    assert s.lock.legs[0].state is not LegState.DONE
    nxt = w.rt.view().occurrences[0].next_good
    assert nxt is not None and nxt.legs[0].train.planned_departure == at("08:44")
    w.clock.set("08:42")  # leg 1 done at UT
    lock = s.lock
    done = replace(lock.legs[0], state=LegState.DONE)
    w.set_occ(replace(s, lock=replace(lock, legs=(done, *lock.legs[1:]))))
    nxt = w.rt.view().occurrences[0].next_good
    assert nxt is not None and nxt.legs[0].board.code == "UT"
    assert nxt.legs[0].train.planned_departure == at("09:16")


async def test_view_next_good_is_none_without_answers_or_kept_leg(tmp_path):
    w = await locked(tmp_path)
    s = only_occurrence(w.rt)
    ctx = w.rt._ctx(s, w.clock.now())
    assert occurrence_view(s, ctx, ()).next_good is None
    assert [x.state for x in occurrence_view(s, ctx, ()).legs] == ["missing", "missing"]
    done = replace(s.lock.legs[0], state=LegState.DONE)
    w.set_occ(replace(s, lock=replace(s.lock, legs=(done, *s.lock.legs[1:]))))
    _, nxt = ONE_TRANSFER  # the kept 08:12 is not in the answer: "check the NS app"
    assert w.view_with([nxt]).next_good is None


async def test_view_route_changed_flag_follows_live_fact(tmp_path):
    w = await locked(tmp_path)
    s = only_occurrence(w.rt)
    assert not w.rt.view().occurrences[0].route_changed
    live = {**s.alert_keys, "live:route_changed:2026-10-27:x": "1"}
    w.set_occ(replace(s, alert_keys=live))
    assert w.rt.view().occurrences[0].route_changed
    sent_only = {**s.alert_keys, "sent:route_changed:2026-10-27:x": "loud"}
    w.set_occ(replace(s, alert_keys=sent_only))
    assert not w.rt.view().occurrences[0].route_changed


async def test_view_done_with_retained_is_not_resolved(tmp_path):
    w = await locked(tmp_path)
    s = only_occurrence(w.rt)
    w.set_occ(replace(s, direction_state=DirectionState.DONE, retained=(s.lock,)))
    o = w.rt.view().occurrences[0]
    assert not o.resolved and len(o.retained) == 1
    assert [x.train_number for x in o.retained[0]] == ["1234", "5678"]
    w.set_occ(replace(s, direction_state=DirectionState.DONE))
    o = w.rt.view().occurrences[0]
    assert o.resolved and o.next_good is None


async def test_view_before_start_has_started_false_and_no_directions(tmp_path):
    v = World(tmp_path).rt.view()
    assert not v.started
    assert (v.directions, v.profiles, v.occurrences) == ((), (), ())


def ledger_with(per_date) -> LookAheadLedger:
    return LookAheadLedger(per_date, (), None, 0, None)


def test_works_ahead_lists_warned_dates_from_today():
    ledger = ledger_with(per_date={"2026-10-28|1": ("warn:a",), "2026-10-29|j": ("warn:b", "restored:b"),
                                   "2026-10-26|1": ("warn:c",), "2026-10-30|j": ("warn:d",)})  # fmt: skip
    assert works_ahead(ledger, date(2026, 10, 27)) == (
        WorksDay(date(2026, 10, 28), 1),
        WorksDay(date(2026, 10, 30), None),
    )


def test_works_ahead_reads_owned_marks_and_ignores_others():
    ledger = ledger_with(
        {"2026-10-28|j": ("warn:a#ob1", "loud:a#ob1"), "2026-10-29|0": ("seed:1",)}
    )
    assert works_ahead(ledger, date(2026, 10, 27)) == (WorksDay(date(2026, 10, 28), None),)


def test_moved_matches_check_profile_text():
    cfg, errors = load_config(PROFILE.replace("NAME", "office_a"), None, None, date(2026, 10, 27))
    assert cfg is not None and not errors
    sig = Signature(
        (
            SignatureLeg("AAA", "UT", time(8, 15), 0, 28),
            SignatureLeg("UT", "RTD", time(8, 50), 0, 34),
        )
    )
    hist = BaselineHistory((BaselineVersion(1, sig, date(2026, 11, 3), True),))
    text = "office_a.outbound: NS moved your 08:12 to 08:15 from 2026-11-03; update your profile?"
    assert moved(cfg.profiles, {"p1|outbound": hist}) == (Moved("p1", Direction.OUTBOUND, text),)
    unconfirmed = BaselineHistory((replace(hist.versions[0], confirmed=False),))
    assert moved(cfg.profiles, {"p1|outbound": unconfirmed}) == ()


async def test_removed_profile_that_still_runs_keeps_direction_and_profile(tmp_path):
    w = await locked(tmp_path)
    w.write(WITHOUT_P1)
    await w.rt.reload()
    assert w.rt.refusal is None and [p.id for p in w.rt.config.profiles] == ["p2"]
    v = w.rt.view()
    assert [p.id for p in v.profiles] == ["p2", "p1"]
    assert v.profiles[1].name == "office_a"
    p1 = [(d.direction, d.max_transfers) for d in v.directions if d.profile_id == "p1"]
    assert p1 == [(Direction.OUTBOUND, 1), (Direction.RETURN, 0)]
    assert [o.key.profile_id for o in v.occurrences] == ["p1"]


async def test_directions_keep_max_transfers_of_running_revision(tmp_path):
    w = await locked(tmp_path)
    w.write(DIRECT)
    await w.rt.reload()
    assert w.rt.refusal is None and w.rt.config.profiles[0].outbound.transfers == ()
    v = w.rt.view()
    assert [d.max_transfers for d in v.directions if d.direction is Direction.OUTBOUND
            and d.profile_id == "p1"] == [1]  # fmt: skip
    assert v.occurrences[0].legs[1].board == "UT"


async def test_undelivered_counts_failed_obligations(tmp_path):
    w = World(tmp_path)
    w.notifier.fails = lambda e: True
    await w.start()
    v = w.rt.view()
    assert v.undelivered > 0 and v.undelivered == len(w.rt.state.obligations)
    assert v.started and v.store_ok and not v.key_rejected and v.requests > 0
    w.notifier.fails = lambda e: False
    await w.rt.watchdog_once()
    assert w.rt.view().undelivered == 0


async def test_view_location_epochs_is_a_copy(tmp_path):
    w = await World(tmp_path).start()
    w.rt._loc_epoch["p1"] = 3
    v = w.rt.view()
    w.rt._loc_epoch["p1"] = 4
    assert v.location_epochs == {"p1": 3}


async def test_view_lookahead_last_ok_lists_directions_never_run(tmp_path):
    w = await World(tmp_path).start()
    assert w.rt.view().lookahead_last_ok == {
        "p1|outbound": None,
        "p1|return": None,
        "p2|outbound": None,
        "p2|return": None,
    }


async def test_view_lookahead_last_ok_stays_old_after_a_failed_works_lookup(tmp_path):
    w = await World(tmp_path, now="05:50").start()
    await w.rt.lookahead_once()
    good = w.clock.now()
    assert w.rt.view().lookahead_last_ok["p1|outbound"] == good

    async def unknown() -> PlannedWorks:
        return PlannedWorks((), "unknown")

    w.provider.planned_works = unknown  # type: ignore[method-assign]
    w.clock.set("05:55")
    await w.rt.lookahead_once()
    assert w.rt.view().lookahead_last_ok["p1|outbound"] == good

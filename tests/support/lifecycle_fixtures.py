"""Shared lifecycle fixtures: the usual two-leg journey, its variants and the poll helpers."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time

from builders import AMS, make_connection, make_leg
from lookahead_fixtures import NEXT_TUE, NOW, OUT, P, confirmed, ledger

from train_commute_companion.lifecycle import (
    PollAnswers,
    Transition,
    journey_of,
    on_location,
    on_poll,
)
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import (
    AdviceRecord,
    AlertEvent,
    Config,
    Connection,
    Ctx,
    Direction,
    DirectionState,
    EventKind,
    Leg,
    LegState,
    LocationSettings,
    Obligation,
    Severity,
    WebhookCopy,
)
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.state import (
    BaselineHistory,
    Commitment,
    Journey,
    LockSource,
    OccurrenceState,
    PollRecord,
)
from train_commute_companion.store import PersistedState

PROFILES = """
profiles:
  - id: "p1"
    name: "office_a"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "RTD"
    transfer_min: {UT: UT_MIN}
    outbound: {first_train: "08:12", buffer_min: 10, transfers: TRANSFERS}
    return: {first_train: "17:52", buffer_min: 5}
"""


DAY = date(2026, 10, 27)


def at(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-27 {hhmm}").replace(tzinfo=AMS)


def ctx(now: datetime, transfers: str = "[UT]", ut_min: int = 4) -> Ctx:
    text = PROFILES.replace("TRANSFERS", transfers).replace("UT_MIN", str(ut_min))
    ps, errs = parse_profiles(text, None)
    assert errs == []
    p = ps[0]
    return Ctx(p, Direction.OUTBOUND, dated_schedule(p, Direction.OUTBOUND, DAY, None), None, now)


DIRECT = "[]"


def conn(*legs: Leg) -> Connection:
    return make_connection(legs)


def ans(origin: str, *conns: Connection, when: str = "08:00") -> ProviderAnswer:
    q = Query(origin, "RTD", at(when))
    return ProviderAnswer(q, conns, q.at)


def poll_answers(s: OccurrenceState, *answers: ProviderAnswer) -> PollAnswers:
    return PollAnswers(answers, (PollRecord(at("08:00"), True, None),), s.generation)


def poll(s: OccurrenceState, c: Ctx, *answers: ProviderAnswer) -> Transition:
    return on_poll(s, c, poll_answers(s, *answers))


def journey(
    legs: Sequence[Leg], states: Sequence[LegState] = (), source: LockSource = LockSource.AUTO
) -> Journey:
    j = journey_of(conn(*legs), source, Commitment.INTENDED)
    xs = [replace(x, state=st) for x, st in zip(j.legs, states, strict=False)]
    return replace(j, legs=(*xs, *j.legs[len(xs) :]))


def occ(lock: Journey | None, **kw: object) -> OccurrenceState:
    base = OccurrenceState(
        key=ctx(at("07:12")).schedule.key,
        generation=1,
        route_revision=1,
        settings_revision=1,
        baseline_version=None,
        direction_state=DirectionState.LOCKED,
        lock=lock,
        retained=(),
        travelling=False,
        tap_at=None,
        at_station=False,
        errand_deadline=None,
        not_travelling=False,
        cancellation_history=(),
        missing_sent=False,
        all_good_evaluated=False,
    )
    s = replace(base, **kw)  # type: ignore[arg-type]
    departed = s.retained or (lock and any(x.state in RIDDEN for x in lock.legs))
    if departed and "direction_state" not in kw:  # a consistent state: riding once leg 1 left
        s = replace(s, direction_state=DirectionState.RIDING)
    return s


RIDDEN = (LegState.RIDING, LegState.DONE)


def numbers(s: OccurrenceState) -> list[str]:
    assert s.lock is not None
    return [x.train.train_number for x in s.lock.legs]


def kinds(t: Transition) -> list[tuple[EventKind, Severity]]:
    return [(e.kind, e.severity) for e in t.events]


# two-leg usual journey AAA -> UT -> RTD
L1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234")


L2 = make_leg("UT", "RTD", "08:50", "09:24", number="5678")


L1X = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True)


L2X = make_leg("UT", "RTD", "08:50", "09:24", number="5678", cancelled=True)


R2 = make_leg("UT", "RTD", "09:05", "09:39", number="6666")


LATER = conn(make_leg("AAA", "UT", "08:25", "08:53", number="1300"), R2)


RIDE = (LegState.RIDING,)


# direct usual journey AAA -> RTD calling at BBB and UT
def direct_1234(cut_after: str | None = None) -> Leg:
    return make_leg(
        "AAA", "RTD", "08:12", "09:24", number="1234", stops=["BBB", "UT"], cut_after=cut_after
    )


T_FULL, T_UT, T_BBB = direct_1234(), direct_1234("UT"), direct_1234("BBB")


FROM_UT = make_leg("UT", "RTD", "08:30", "09:00", number="8888")


FROM_BBB = make_leg("BBB", "RTD", "08:35", "09:10", number="9999")


def usual_locked() -> OccurrenceState:
    return occ(journey([L1, L2]))


B1 = make_leg("BBB", "UT", "08:30", "08:45", number="2001")


def direct_riding() -> OccurrenceState:
    return occ(journey([T_FULL], RIDE))


def cut_at_ut(recovery: LegState) -> OccurrenceState:
    j = journey([T_FULL, FROM_UT], (LegState.RIDING, recovery))
    cut = replace(j.legs[0], served_endpoint="UT", alight_stop="UT")
    return occ(replace(j, legs=(cut, j.legs[1])))


def with_track(leg: Leg, planned: str, actual: str) -> Leg:
    b = replace(leg.stops[0], planned_track=planned, actual_track=actual)
    return replace(leg, stops=(b, *leg.stops[1:]))


USUAL = conn(L1, L2)


DONE_RIDE = (LegState.DONE, LegState.RIDING)


def accepted_first(j: Journey) -> Journey:
    x = replace(j.legs[0], state=LegState.DONE, accepted_at=at("08:50"), accept_value=at("08:40"))
    return replace(j, legs=(x, *j.legs[1:]))


def polls(s: OccurrenceState, *whens: str, c: object = USUAL) -> OccurrenceState:
    for w in whens:
        s = poll(s, ctx(at(w)), ans("AAA", c, when=w)).state  # type: ignore[arg-type]
    return s


def arrived() -> OccurrenceState:
    """The last leg (09:24) accepted at 09:33, watched until 10:03."""
    s = polls(occ(accepted_first(journey([L1, L2], DONE_RIDE))), "09:29", "09:33")
    assert s.lock is not None and s.lock.legs[-1].state is LegState.DONE
    return s


def report(s: OccurrenceState, zone: str, when: str) -> OccurrenceState:
    e = LocationEvent(zone, "report", at(when))
    return on_location(s, ctx(at(when)), e).state


def retained_of(j: Journey) -> OccurrenceState:
    """The lock moved to the 08:25; the user may still be on `j`."""
    return occ(journey(list(LATER.legs)), retained=(j,))


def full_state() -> PersistedState:
    """One of every persisted type, every optional field filled."""
    p = replace(
        P,
        extra_dates=frozenset({date(2026, 12, 24)}),
        location=LocationSettings("ha", "tracker", {"UT": "zone.ut"}),
        alerts=replace(
            P.alerts, latest_acceptable_arrival=time(10, 0), quiet_hours=(time(22), time(7))
        ),
    )
    cfg = Config(2, (p,))
    event = AlertEvent(
        "p1",
        "office_a",
        OUT,
        NEXT_TUE,
        1,
        EventKind.DELAYED,
        Severity.SILENT,
        L1.train,
        "t",
        "k",
        NOW,
    )
    ob = Obligation("o", event, None, ("p1", OUT, NEXT_TUE), 1, 3, "f", NOW, 2, True)
    copy = WebhookCopy("tcc_x", ob, ("ab12",), NOW, NOW)
    retained = journey([L1, L2], RIDE)
    legs = (
        replace(retained.legs[0], onward_from=at("08:44"), accepted_at=at("08:50")),
        *retained.legs[1:],
    )
    s = occ(
        journey([L1, L2], RIDE),
        retained=(replace(retained, legs=legs),),
        cancellation_history=legs,
        alert_keys={"a": "b"},
        advice=AdviceRecord(L2.train, at("08:00"), False, delivered=True),
        poll_records=(PollRecord(at("08:00"), False, "rate"),),
        zone_reports={"UT": at("08:51")},
        tap_at=at("08:05"),
    )
    hist = BaselineHistory((confirmed(1), confirmed(2, date(2026, 11, 1))), floor=4)
    led = ledger(hist, per_date={"2026-11-03|j": ("a", "b")}, pending=(ob,), last_run=NOW)
    return PersistedState(
        cfg, {1: Config(1, ()), 2: cfg}, {"p1": hist}, {"p1": led}, {"k": s}, (ob,), True,
        (copy,),
    )  # fmt: skip

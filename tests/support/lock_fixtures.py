"""Shared auto-lock fixtures: the profile with its usual and later connections."""

from datetime import date, datetime

from builders import AMS, make_connection, make_leg

from train_commute_companion.model import (
    Connection,
    Ctx,
    Direction,
    DirectionState,
)
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import ProviderAnswer, Query
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.state import OccurrenceState

PROFILES = """
profiles:
  - id: "p1"
    name: "office_a"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "UT"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""


DAY = date(2026, 10, 27)


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=AMS)


def at(hhmm: str) -> datetime:
    return dt(f"2026-10-27 {hhmm}")


def prof():
    ps, errs = parse_profiles(PROFILES, None)
    assert errs == []
    return ps[0]


def ctx(now: datetime, day: date = DAY) -> Ctx:
    p = prof()
    return Ctx(p, Direction.OUTBOUND, dated_schedule(p, Direction.OUTBOUND, day, None), None, now)


def fresh(day: date = DAY) -> OccurrenceState:
    return OccurrenceState(
        key=ctx(at("07:12"), day).schedule.key,
        generation=0,
        route_revision=1,
        settings_revision=1,
        baseline_version=None,
        direction_state=DirectionState.IDLE,
        lock=None,
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


def direct(dep: str, arr: str, number: str = "1234", day: date = DAY) -> Connection:
    return make_connection([make_leg("AAA", "UT", dep, arr, number=number, day=day)])


def answer(*conns: Connection) -> ProviderAnswer:
    q = Query("AAA", "UT", at("07:12"))
    return ProviderAnswer(q, conns, q.at)


USUAL = direct("08:12", "08:46", "1234")


LATER = direct("08:44", "09:24", "5678")

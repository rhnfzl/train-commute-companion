"""Shared look-ahead fixtures: a two-leg AAA -> UT -> RTD outbound journey at 08:12."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta

from builders import AMS, make_connection, make_leg

from train_commute_companion.lookahead import (
    BaselineHistory,
    BaselineVersion,
    LookAheadLedger,
    ProbeResult,
    evaluate,
    run_lookahead,
    signature_of,
)
from train_commute_companion.model import Direction
from train_commute_companion.profile import parse_profiles
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, Query

OUT = Direction.OUTBOUND
MON = date(2026, 10, 26)  # probe days: Tue 27 Oct, Wed 28 Oct, Tue 3 Nov
TUE, WED, NEXT_TUE = date(2026, 10, 27), date(2026, 10, 28), date(2026, 11, 3)
DAYS = (TUE, WED, NEXT_TUE)
NOW = datetime(2026, 10, 26, 6, 0, tzinfo=AMS)
CLEAR = PlannedWorks((), "complete")
UNKNOWN = PlannedWorks((), "unknown")

YAML = """
profiles:
  - id: "p1"
    name: "office_a"
    days: [tue, wed]
    home_station: "AAA"
    work_station: "RTD"
    outbound: {first_train: "08:12", transfers: ["UT"], buffer_min: 10}
    return: {first_train: "17:52", transfers: ["UT"], buffer_min: 5}
    transfer_min: {UT: 4}
"""


def profile(**out: object):
    ps, errs = parse_profiles(YAML, None)
    assert errs == []
    p = ps[0]
    return replace(p, outbound=replace(p.outbound, **out)) if out else p


P = profile()


def ans(o: str, d: str, *conns) -> ProviderAnswer:
    return ProviderAnswer(Query(o, d, NOW), tuple(conns), NOW)


def legs(day: date, l1=("08:12", "08:40"), l2=("08:46", "09:24"), **kw):
    if kw.get("night"):  # builders put times before 04:00 on the next day
        day = day - timedelta(days=1)
    a = make_leg("AAA", "UT", *l1, day=day, number="1000", cancelled=kw.get("cancel1", False))
    b = make_leg(
        "UT",
        "RTD",
        *l2,
        day=day,
        number="2000",
        cancelled=kw.get("cancel2", False),
        stops=kw.get("stops2", ()),
        cut_after=kw.get("cut2"),
    )
    return a, b


def day_answers(day: date, **kw) -> list[ProviderAnswer]:
    a, b = legs(day, **kw)
    return [
        ans("AAA", "UT", make_connection([a])),
        ans("UT", "RTD", make_connection([b])),
        ans("AAA", "RTD", make_connection([a, b])),
    ]


def probe(day: date, base: BaselineVersion | None = None, works=CLEAR, p=P, **kw):
    return evaluate(p, OUT, day, day_answers(day, **kw), works, base)


def normal_signature(day: date = TUE, **kw):
    a, b = legs(day, **kw)
    return signature_of(make_connection([a, b]), day)


def confirmed(version: int = 1, eff: date = date(2026, 10, 1), **kw) -> BaselineVersion:
    return BaselineVersion(version, normal_signature(**kw), eff, True)


def ledger(history: BaselineHistory, revision: int = 1, **kw) -> LookAheadLedger:
    return LookAheadLedger(
        kw.get("per_date", {}), kw.get("pending", ()), kw.get("last_run"), revision, history.latest
    )


def run(history: BaselineHistory, results: list[ProbeResult], led=None, today=MON, now=NOW, p=P):
    out = run_lookahead(p, OUT, history, led or ledger(history), results, 1, today, now)
    assert out is not None
    return out


def first_train(history: BaselineHistory, day: date) -> time | None:
    v = history.in_force(day)
    return None if v is None else v.signature.legs[0].departure

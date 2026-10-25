from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from train_commute_companion.model import (
    Connection,
    Leg,
    Provenance,
    Stop,
    TimeObs,
    TrainId,
)

AMS = ZoneInfo("Europe/Amsterdam")


def _at(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    d = day + timedelta(days=1) if h < 4 else day  # before 04:00 belongs to the next day
    return datetime.combine(d, time(h, m), tzinfo=AMS)


def _stop(
    code: str,
    arr: datetime | None,
    dep: datetime | None,
    delay_arr: int,
    delay_dep: int,
    provenance: Provenance,
    cancelled: bool,
) -> Stop:
    def obs(planned: datetime | None, delay: int) -> TimeObs | None:
        if planned is None:
            return None
        prov = Provenance.PLANNED if cancelled else provenance  # a cancelled stop reports nothing
        value = None if prov is Provenance.MISSING else planned + timedelta(minutes=delay)
        return TimeObs(planned, value, prov, planned)

    return Stop(
        code, code, arr, dep, obs(arr, delay_arr), obs(dep, delay_dep), cancelled, False, None, None
    )


def _cut(s: Stop) -> Stop:
    def plan(o: TimeObs | None) -> TimeObs | None:
        return o and TimeObs(o.planned, None, Provenance.PLANNED, o.observed_at)

    return replace(s, cancelled=True, arrival=plan(s.arrival), departure=plan(s.departure))


def make_leg(
    board: str,
    alight: str,
    dep: str,
    arr: str,
    *,
    number: str = "1234",
    day: date = date(2026, 10, 27),
    delay_dep: int = 0,
    delay_arr: int = 0,
    cancelled: bool = False,
    provenance: Provenance = Provenance.REPORTED,
    stops: Sequence[str] = (),
    cut_after: str | None = None,
    transfer_time: int | None = None,
) -> Leg:
    t_dep, t_arr = _at(day, dep), _at(day, arr)
    mids = [_stop(c, t_dep, t_dep, delay_dep, delay_dep, provenance, cancelled) for c in stops]
    chain = [
        _stop(board, None, t_dep, 0, delay_dep, provenance, cancelled),
        *mids,
        _stop(alight, t_arr, None, delay_arr, 0, provenance, cancelled),
    ]
    if cut_after is not None:
        # stops after cut_after are cancelled
        idx = next(i for i, s in enumerate(chain) if s.code == cut_after)
        chain = [s if i <= idx else _cut(s) for i, s in enumerate(chain)]
    return Leg(
        train=TrainId(day, number, t_dep),
        category="IC",
        stops=tuple(chain),
        cancelled=cancelled,
        part_cancelled=cut_after is not None,
        change_possible=None,
        reachable=None,
        alternative_transport=False,
        messages=(),
        transfer_time_to_next=transfer_time,
        transfer_message_min=None,
        cross_platform=None,
    )


def make_connection(legs: Sequence[Leg], status: str = "NORMAL") -> Connection:
    return Connection(tuple(legs), status, None)

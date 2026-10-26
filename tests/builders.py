from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.model import (
    Connection,
    Direction,
    Leg,
    Provenance,
    Stop,
    TimeObs,
    TrainId,
)
from train_commute_companion.runtime import Runtime

AMS = ZoneInfo("Europe/Amsterdam")


def _at(day: date, hhmm: str, fold: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    d = day + timedelta(days=1) if h < 4 else day  # before 04:00 belongs to the next day
    return datetime.combine(d, time(h, m, fold=fold), tzinfo=AMS)  # fold=1: the second 02:xx


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
        # add in UTC: local datetime arithmetic drops the DST fold
        later = (planned.astimezone(UTC) + timedelta(minutes=delay)).astimezone(AMS)
        value = None if prov is Provenance.MISSING else later
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
    fold_dep: int = 0,
    fold_arr: int = 0,
) -> Leg:
    t_dep, t_arr = _at(day, dep, fold_dep), _at(day, arr, fold_arr)
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


def _on_track(leg: Leg, track: str) -> Leg:
    board = replace(leg.stops[0], actual_track=track)
    return replace(leg, stops=(board, *leg.stops[1:]))


# 08:12 AAA-UT, then the 08:46 UT-RTD 5 min late on new track 7; the next connection 08:44
ONE_TRANSFER: tuple[Connection, ...] = (
    make_connection(
        [
            make_leg("AAA", "UT", "08:12", "08:40", number="1234"),
            _on_track(
                make_leg("UT", "RTD", "08:46", "09:20", number="5678", delay_dep=5, delay_arr=5),
                "7",
            ),
        ]
    ),
    make_connection(
        [
            make_leg("AAA", "UT", "08:44", "09:08", number="1236"),
            make_leg("UT", "RTD", "09:16", "09:50", number="5680"),
        ]
    ),
)


def only_occurrence(rt: Runtime, direction: Direction = Direction.OUTBOUND) -> OccurrenceState:
    """The single unarchived occurrence of `direction`."""
    mine = [s for s in rt.state.occurrences.values() if s.key.direction is direction]
    assert len(mine) == 1, mine
    return mine[0]

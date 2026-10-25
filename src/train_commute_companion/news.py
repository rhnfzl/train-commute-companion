"""What is news: alert keys, severity table, escalation, all_good (7.5, 7.9, 7.12 rules 4, 20)."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, timedelta

from train_commute_companion.classify import classify_leg, find_leg
from train_commute_companion.health import board_health
from train_commute_companion.lifecycle import Board, Ctx, Journey, OccurrenceState, _event
from train_commute_companion.messages import body
from train_commute_companion.model import (
    AlertEvent,
    ClassifiedLeg,
    EventKind,
    LegKind,
    Observation,
    Provenance,
    Severity,
    TrainId,
    TransferCheck,
    TransferState,
)
from train_commute_companion.transfer import check_transfer

AT_RISK_LOUD_MIN = 30
PLATFORM_LOUD_MIN = 15
_SILENT = frozenset(
    {
        EventKind.CAN_SEE_TRAIN_AGAIN,
        EventKind.ALL_GOOD,
        EventKind.NEARLY_ON_TIME,
        EventKind.BACK_TO_NORMAL,
        EventKind.RESTORED,
        EventKind.TIMETABLE_ADOPTED,
    }
)


def _step(delay: int | None) -> int:
    """3-min steps; 1 min or less counts as on time, so a +2/+3 wobble never flips the key."""
    return 0 if delay is None or delay <= 1 else max(delay // 3, 1)


def alert_key(
    lock: Journey,
    classified: Sequence[ClassifiedLeg],
    transfers: Sequence[TransferCheck],
    relock_target: TrainId | None,
) -> str:
    """Everything a message states, per leg and per transfer; only a change is news."""
    legs = [
        "/".join(
            (
                c.leg.train.train_number,
                c.kind.value,
                str(_step(c.dep_delay_min)),
                str(_step(c.arr_delay_min)),
                str(c.leg.board.actual_track or c.leg.board.planned_track),
                c.last_served.code if c.last_served else "-",
            )
        )
        for c in classified
    ]
    target = relock_target.train_number if relock_target else "-"
    return "|".join(
        [
            lock.legs[0].train.operating_date.isoformat(),
            *legs,
            *(t.state.value for t in transfers),
            target,
        ]
    )


def severity(
    kind: EventKind,
    *,
    minutes_to_departure: int | None,
    breaks_transfer: bool,
    past_latest_arrival: bool,
    is_tomorrow: bool,
) -> Severity:
    """Table 7.9. A kind without a rule is loud: unmapped is loud."""
    soon = minutes_to_departure is None  # unknown timing errs on the loud side
    if kind is EventKind.TRANSFER_AT_RISK:
        loud = soon or minutes_to_departure <= AT_RISK_LOUD_MIN  # type: ignore[operator]
    elif kind is EventKind.PLATFORM_CHANGE:
        loud = soon or minutes_to_departure <= PLATFORM_LOUD_MIN  # type: ignore[operator]
    elif kind is EventKind.DELAYED:
        loud = breaks_transfer or past_latest_arrival
    elif kind is EventKind.WORKS_AHEAD:
        loud = is_tomorrow
    else:
        loud = kind not in _SILENT
    return Severity.LOUD if loud else Severity.SILENT


def escalations(
    ledger: Mapping[str, Severity], facts: Sequence[tuple[str, EventKind]], ctx: Ctx
) -> list[str]:
    """Fact keys sent silent that now cross a loud boundary (rule 4); each goes out once more."""
    gap = ctx.schedule.first_departure.astimezone(UTC) - ctx.now.astimezone(UTC)
    minutes = int(gap // timedelta(minutes=1))
    tomorrow = ctx.schedule.key.service_date > ctx.now.date()
    return [
        key
        for key, kind in facts
        if ledger.get(key) is Severity.SILENT
        and severity(
            kind,
            minutes_to_departure=minutes,
            breaks_transfer=False,
            past_latest_arrival=False,
            is_tomorrow=tomorrow,
        )
        is Severity.LOUD
    ]


def unsent(ledger: Mapping[str, Severity], events: Sequence[AlertEvent]) -> list[AlertEvent]:
    """Ongoing facts repeat on every poll with the same key; only a key not yet sent is news."""
    out: dict[str, AlertEvent] = {}
    for e in events:
        if e.key not in ledger:
            out.setdefault(e.key, e)
    return list(out.values())


def all_good(s: OccurrenceState, board: Board, ctx: Ctx) -> AlertEvent | None:
    """Evaluated once, at the first lock (Q4, OQ3 = A). The caller sets `all_good_evaluated`."""
    if s.all_good_evaluated or s.lock is None or not board.answers:
        return None
    if not board_health(board.polls, ctx.now).fresh:
        return None
    # newest answer first: a leg is judged on the latest answer that shows it, never an older one
    answers = sorted(board.answers, key=lambda a: a.fetched_at.astimezone(UTC), reverse=True)
    seen: list[ClassifiedLeg] = []
    for x in s.lock.legs:
        found = next(
            (
                leg
                for a in answers
                for leg, obs in [find_leg(a, x.train, x.board_station)]
                if obs is Observation.SEEN and leg is not None
            ),
            None,
        )
        if found is None:
            return None
        c = replace(classify_leg(found, x.alight_stop), observation=Observation.SEEN)
        times = (found.board.departure, found.alight.arrival)
        if c.kind is not LegKind.OK or any(
            t is None or t.provenance is not Provenance.REPORTED for t in times
        ):
            return None
        seen.append(c)
    for a, b, x in zip(seen, seen[1:], s.lock.legs, strict=False):
        if (
            check_transfer(a, b, x.alight_stop, ctx.profile.transfer_min).state
            is not TransferState.OK
        ):
            return None
    legs = [c.leg for c in seen]
    return _event(
        ctx, EventKind.ALL_GOOD, Severity.SILENT, body(EventKind.ALL_GOOD, legs=legs), legs[0].train
    )

"""Arrival targets for automatic train choices, independent of a provider's search window."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from train_commute_companion.calendar import TZ, to_utc
from train_commute_companion.classify import expected_departure
from train_commute_companion.model import Connection, Ctx, Stop, present
from train_commute_companion.providers.base import Query
from train_commute_companion.transfer import is_reported

ArrivalStatus = Literal["on_target", "late", "unknown"]


def arrival_status(
    deadline: datetime, planned: datetime | None, reported: datetime | None
) -> ArrivalStatus:
    if any(t is not None and to_utc(t) > deadline for t in (planned, reported)):
        return "late"
    return "unknown" if planned is None or reported is None else "on_target"


def planning_query(
    ctx: Ctx, station: str, departure_at: datetime, *, required: tuple[str, ...] = ()
) -> Query:
    """Search by station arrival when a target is active. A station code is the only anchor."""
    p, d = ctx.profile, ctx.direction
    deadline = ctx.schedule.arrival_deadline
    return Query.for_profile(
        p,
        station,
        p.destination(d),
        departure_at if deadline is None else deadline.astimezone(TZ),
        required[0] if deadline is not None and required else None,
        search_for_arrival=deadline is not None,
    )


def current_planning_query(ctx: Ctx, query: Query) -> bool:
    deadline = ctx.schedule.arrival_deadline
    if deadline is None:
        return not query.search_for_arrival
    return query.search_for_arrival and to_utc(query.at) == deadline


@dataclass(frozen=True, slots=True)
class ArrivalChoice:
    deadline: datetime
    first: datetime | None
    last: datetime | None
    prefer_direct_min: int
    required: tuple[str, ...]
    taken: tuple[str, ...] = ()
    before: datetime | None = None
    stay_capped: bool = False

    def accepts(self, c: Connection, *, allow_planned: bool = False) -> bool:
        dep = expected_departure(c)
        planned_dep = to_utc(c.legs[0].train.planned_departure)
        if (
            self.stay_capped
            or (self.first is not None and min(dep, planned_dep) < self.first)
            or (self.last is not None and max(dep, planned_dep) > self.last)
            or (self.before is not None and max(dep, planned_dep) >= self.before)
        ):
            return False
        return self.accepts_arrival(
            c.legs[-1].alight, c.transfer_stations, allow_planned=allow_planned
        )

    def accepts_arrival(
        self, stop: Stop, transfers: tuple[str, ...] = (), *, allow_planned: bool = False
    ) -> bool:
        if self.required and (*self.taken, *transfers) != self.required:
            return False
        reported = _preview_arrival(stop, allow_planned=allow_planned)
        return arrival_status(self.deadline, stop.planned_arrival, reported) == "on_target"

    def pick(self, good: Sequence[Connection], *, allow_planned: bool = False) -> Connection | None:
        def rank(c: Connection) -> tuple[datetime, int, float]:
            arrival = present(_preview_arrival(c.legs[-1].alight, allow_planned=allow_planned))
            return expected_departure(c), -len(c.legs), -to_utc(arrival).timestamp()

        latest = max(good, key=rank, default=None)
        if latest is None or len(latest.legs) == 1 or not self.prefer_direct_min:
            return latest
        floor = expected_departure(latest) - timedelta(minutes=self.prefer_direct_min)
        return max(
            (c for c in good if len(c.legs) == 1 and expected_departure(c) >= floor),
            key=rank,
            default=latest,
        )


def _preview_arrival(stop: Stop, *, allow_planned: bool) -> datetime | None:
    if is_reported(stop.arrival):
        return present(stop.arrival).value
    return stop.planned_arrival if allow_planned else None

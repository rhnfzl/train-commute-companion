"""Preview and apply travel plan edits against the written runtime state."""

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from math import lcm
from typing import Any

from train_commute_companion.calendar import TZ, has_room, to_utc
from train_commute_companion.lifecycle import is_resolved
from train_commute_companion.model import DatedSchedule, Direction, Profile, okey
from train_commute_companion.revisions import occurrence_ctx, profile_for, signature_for
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.serde import encode
from train_commute_companion.state import OccurrenceState
from train_commute_companion.store import PersistedState
from train_commute_companion.travel_plans import (
    PlanReceipt,
    TravelPlan,
    fields,
    overlaps,
    parse_plan,
    plan_dict,
    plan_suppresses,
    plan_text,
    regular_date,
)


def request_hash(change: object) -> str:
    try:
        text = json.dumps(change, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("The travel change must be a JSON object.") from None
    if len(text) > 40_000:
        raise ValueError("The travel change is too large.")
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def _regular_schedules(st: PersistedState, p: Profile, day: date) -> Iterator[DatedSchedule]:
    if regular_date(p, day):
        for direction in Direction:
            service = day + timedelta(days=p.journey(direction).day_offset)
            if not p.paused_on(service):
                sig = signature_for(st, p, direction, service)
                yield dated_schedule(p, direction, day, sig.legs[0].departure if sig else None)


def _pair_days(
    plan: TravelPlan, other: TravelPlan | None, profile: Profile, today: date
) -> Iterator[date]:
    first = max(today - timedelta(days=2), plan.start)
    if other is not None:
        first = max(first, other.start - timedelta(days=2))
    ends = [
        day
        for day in (plan.end, other.end + timedelta(days=2) if other and other.end else None)
        if day is not None
    ]
    last = min(ends, default=None)

    if last is not None and last < first:
        return
    # The weekly phases repeat after their least common multiple. Include a year for DST,
    # and repeat the sample after exceptions that can mask the first matching weeks.
    period = 7 * lcm(plan.interval, other.interval if other else 1)
    exceptions = plan.skip | (other.skip if other else profile.extra_dates)
    horizon = max(380, period + 14)
    span_starts = {first}
    if exceptions and max(exceptions) >= first:
        span_starts.add(max(first, max(exceptions) - timedelta(days=2)))
    if other is None:
        span_starts.update(
            max(first, day + timedelta(days=1)) for _, day in profile.pauses if day >= first
        )
    upper = date(9998, 12, 29)
    seen: set[date] = set()
    for start in sorted(span_starts):
        count = min(horizon, (min(last or upper, upper) - start).days + 1)
        for offset in range(count):
            day = start + timedelta(days=offset)
            if day not in seen and plan.runs(day):
                seen.add(day)
                yield day
    for item in sorted(exceptions):
        for offset in range(-2, 3):
            day = item + timedelta(days=offset)
            if (
                day >= first
                and (last is None or day <= last)
                and day not in seen
                and has_room(day)
                and plan.runs(day)
            ):
                seen.add(day)
                yield day


@dataclass(frozen=True, slots=True)
class Conflict:
    profile_id: str
    name: str
    dates: tuple[str, ...]
    keep: bool
    more_dates: bool


def _conflicts(st: PersistedState, plan: TravelPlan, now: datetime) -> tuple[Conflict, ...]:
    regular = [(p, None) for p in (st.config.profiles if st.config else ())]
    proposed = [(p.profile, p) for p in st.travel_plans.values() if p.id != plan.id]
    result = []
    for profile, other in (*regular, *proposed):
        dates: set[date] = set()
        keep = True
        for day in _pair_days(plan, other, profile, now.astimezone(TZ).date()):
            legs = tuple(s for s in plan.schedules(day) if to_utc(s.window_end) > to_utc(now))
            if not legs:
                continue
            for offset in range(-2, 3):
                target_day = day + timedelta(days=offset)
                targets = (
                    other.schedules(target_day)
                    if other
                    else _regular_schedules(st, profile, target_day)
                )
                for following in targets:
                    if to_utc(following.window_end) <= to_utc(now):
                        continue
                    crossing = any(overlaps(s, following) for s in legs)
                    if offset == 0 or crossing:
                        dates.add(target_day)
                        keep = keep and not crossing
        if dates:
            result.append(
                Conflict(
                    profile.id,
                    profile.name,
                    tuple(d.isoformat() for d in sorted(dates)[:12]),
                    keep,
                    len(dates) > 12
                    or (plan.end is None and (other.end is None if other else bool(profile.days))),
                )
            )
    return tuple(result)


def _safe_to_replace(s: OccurrenceState, now: datetime) -> bool:
    if is_resolved(s):
        return True
    if s.retained or s.travelling or s.at_station:
        return False
    return s.lock is None or to_utc(s.lock.legs[0].train.planned_departure) > to_utc(now)


@dataclass(frozen=True, slots=True)
class PreparedChange:
    previous: TravelPlan | None
    plan: TravelPlan | None
    plan_id: str
    snapshot_hash: str
    request_hash: str
    remove_occurrences: frozenset[str]
    text: str
    saved_text: str

    def review(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "text": self.text,
            "review_token": self.snapshot_hash,
            "plan": plan_dict(self.plan) if self.plan else None,
        }

    def apply(self, st: PersistedState, request_id: str) -> tuple[PersistedState, PlanReceipt]:
        revision = st.travel_revision + 1
        plan = None if self.plan is None else replace(self.plan, revision=revision)
        plans = {k: v for k, v in st.travel_plans.items() if k != self.plan_id}
        if plan is not None:
            plans[self.plan_id] = replace(plan, profile=replace(plan.profile, id=self.plan_id))
        receipt = PlanReceipt(
            self.request_hash,
            revision,
            self.previous,
            plans.get(self.plan_id),
            self.plan_id,
            self.saved_text,
        )
        remove = self.remove_occurrences
        return replace(
            st,
            travel_plans=plans,
            travel_receipts={**st.travel_receipts, request_id: receipt},
            travel_revision=revision,
            occurrences={k: s for k, s in st.occurrences.items() if k not in remove},
            obligations=tuple(
                o
                for o in st.obligations
                if o.occurrence is None or okey(o.occurrence) not in remove
            ),
            journey_records={
                k: record for k, record in st.journey_records.items() if k not in remove
            },
        ), receipt


def prepare(
    st: PersistedState, raw: object, now: datetime, known: frozenset[str] | None
) -> PreparedChange | dict[str, Any]:
    try:
        return _prepare(st, raw, now, known)
    except ValueError as error:
        return {"status": "invalid", "text": str(error)}


def _prepare(
    st: PersistedState, raw: object, now: datetime, known: frozenset[str] | None
) -> PreparedChange | dict[str, Any]:
    digest = request_hash(raw)
    data = fields(raw, "action plan plan_id conflict_policy undo_id", "Travel change")
    action = data.get("action")
    previous = None
    plan = None
    conflicts: tuple[Conflict, ...] = ()
    if action in ("create", "edit"):
        fields(
            data,
            "action plan conflict_policy" + (" plan_id" if action == "edit" else ""),
            "Travel change",
        )
        if action == "edit":
            previous = _existing(st, data.get("plan_id"))
        pid = previous.id if previous else "travel-pending"
        plan = parse_plan(
            data.get("plan"),
            pid,
            st.travel_revision + 1,
            st.config.profiles if st.config else (),
            known,
            now,
        )
    elif action == "cancel":
        fields(data, "action plan_id", "Cancel")
        previous = _existing(st, data.get("plan_id"))
        pid = previous.id
    elif action == "undo":
        fields(data, "action undo_id conflict_policy", "Undo")
        uid = data.get("undo_id")
        receipt = st.travel_receipts.get(uid) if isinstance(uid, str) else None
        if receipt is None or any(
            r.revision > receipt.revision and r.plan_id == receipt.plan_id
            for r in st.travel_receipts.values()
        ):
            raise ValueError("This change can no longer be undone. Review the current plan.")
        pid, previous, plan = receipt.plan_id, receipt.applied, receipt.previous
    else:
        raise ValueError("Choose create, edit, cancel, or undo.")

    policy = data.get("conflict_policy")
    if policy not in (None, "replace", "keep"):
        raise ValueError("The conflict policy must be replace or keep.")
    if plan is not None:
        conflicts = _conflicts(st, plan, now)
        authorized = plan.replaces
        unresolved = tuple(c for c in conflicts if c.profile_id not in authorized)
        if unresolved and (
            policy is None or policy == "keep" and not all(c.keep for c in unresolved)
        ):
            return _conflict_result(plan, unresolved)
        if policy == "replace":
            plan = replace(
                plan, replaces=tuple(sorted(set(authorized) | {c.profile_id for c in conflicts}))
            )
    remove = []
    for key, s in st.occurrences.items():
        if is_resolved(s):
            if plan is not None and s.key.profile_id == pid:
                travel_day = s.key.service_date - timedelta(
                    days=plan.profile.journey(s.key.direction).day_offset
                )
                if any(
                    sch.key == s.key and to_utc(sch.window_end) > to_utc(now)
                    for sch in plan.schedules(travel_day)
                ):
                    raise ValueError(
                        f"{s.key.direction.value.capitalize()} on {s.key.service_date} "
                        "was already completed or skipped. Create another plan for a new journey."
                    )
            continue
        p = profile_for(st, s)
        ctx = occurrence_ctx(st, s, now) if p is not None else None
        if s.key.profile_id == pid or (
            plan is not None and p and ctx and plan_suppresses(ctx.schedule, p, (plan,))
        ):
            if not _safe_to_replace(s, now):
                raise ValueError(
                    f"{p.name if p else s.key.profile_id} is actively being followed. "
                    "Wait for that journey to end before changing it."
                )
            remove.append(key)

    # Bind the preview to current plan/configuration contents and affected windows without
    # tying a user approval to normal watch ticks or selection of an unboarded train.
    seal = request_hash(
        {
            "change": digest,
            "route_revision": st.config.revision if st.config else None,
            "plan_revision": st.travel_revision,
            "plan": encode(plan),
            "conflicts": encode(conflicts),
        }
    )
    verb, done = {
        "create": ("Save", "Saved"),
        "edit": ("Update", "Updated"),
        "undo": ("Undo", "Undid"),
        "cancel": ("Cancel", "Cancelled"),
    }[action]
    shown = plan or previous
    details = plan_text(shown) if shown is not None else "travel plan."
    if plan is not None and plan.replaces:
        selected = [c for c in conflicts if c.profile_id in plan.replaces]
        if selected:
            details += " Replaces " + "; ".join(_conflict_text(c) for c in selected) + "."
    return PreparedChange(
        previous,
        plan,
        pid,
        seal,
        digest,
        frozenset(remove),
        f"{verb} {details}",
        f"{done} {details}",
    )


def _existing(st: PersistedState, pid: object) -> TravelPlan:
    plan = st.travel_plans.get(pid) if isinstance(pid, str) else None
    if plan is None:
        raise ValueError("That travel plan was not found.")
    return plan


def _conflict_text(c: Conflict) -> str:
    return f"{c.name} on {', '.join(c.dates)}" + (" and more dates" if c.more_dates else "")


def _conflict_result(plan: TravelPlan, conflicts: tuple[Conflict, ...]) -> dict[str, Any]:
    names = "; ".join(_conflict_text(c) for c in conflicts)
    keep = all(c.keep for c in conflicts)
    text = f"{plan.profile.name} conflicts with {names}. Choose Replace or edit the plan."
    if keep:
        text = (
            f"{plan.profile.name} also runs alongside {names}. Choose Replace, Keep both, or edit."
        )
    return {
        "status": "conflict",
        "text": text,
        "conflicts": [encode(c) for c in conflicts],
        "can_keep": keep,
    }


def active_travel_schedules(
    plans: tuple[TravelPlan, ...], now: datetime
) -> Iterator[tuple[TravelPlan, DatedSchedule]]:
    today = now.astimezone(TZ).date()
    for plan in plans:
        for back in (-1, 0, 1, 2):
            for sch in plan.schedules(today - timedelta(days=back)):
                if to_utc(sch.window_start) <= to_utc(now) < to_utc(sch.window_end) and not (
                    plan_suppresses(sch, plan.profile, plans)
                ):
                    yield plan, sch

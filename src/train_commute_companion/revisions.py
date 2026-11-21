"""A profile as an occurrence runs it: its route revision with the newest settings, and the schedule
that gives it (7.12 rules 10, 19). Pure: every answer is read from the state it is given."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta

from train_commute_companion.calendar import to_utc
from train_commute_companion.model import (
    Config,
    Ctx,
    Direction,
    Profile,
    Signature,
    UsualJourney,
    direction_key,
    present,
)
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.state import (
    BaselineBinding,
    BaselineHistory,
    BaselineVersion,
    OccurrenceState,
)
from train_commute_companion.store import PersistedState
from train_commute_companion.travel_plans import plan_suppresses


def with_settings(route: Profile, settings: Profile) -> Profile:
    """`route`'s trains with `settings`' settings (7.12 rule 10)."""

    def j(d: Direction) -> UsualJourney:
        return replace(
            route.journey(d),
            buffer_min=settings.journey(d).buffer_min,
            earlier_train_advice=settings.journey(d).earlier_train_advice,
            active_lead_min=settings.journey(d).active_lead_min,
            pick=settings.journey(d).pick,
            min_stay_min=settings.journey(d).min_stay_min,
            arrive_by=settings.journey(d).arrive_by,
            walk_after_min=settings.journey(d).walk_after_min,
            prefer_direct_min=settings.journey(d).prefer_direct_min,
            other_stations=settings.journey(d).other_stations,
        )

    return replace(
        route,
        name=settings.name,
        alerts=settings.alerts,
        brief=settings.brief,
        location=settings.location,
        transfer_min=settings.transfer_min,
        outbound=j(Direction.OUTBOUND),
        return_=j(Direction.RETURN),
    )


def same_route(a: Profile, b: Profile) -> bool:
    return with_settings(a, b) == b


def find_profile(cfg: Config | None, pid: str) -> Profile | None:
    return next((p for p in (cfg.profiles if cfg else ()) if p.id == pid), None)


def effective_profile(
    snaps: Mapping[int, Config], cfg: Config | None, s: OccurrenceState
) -> Profile | None:
    """The occurrence's route revision with the newest settings (7.12 rule 10)."""
    route = find_profile(snaps.get(s.route_revision), s.key.profile_id)
    cur = find_profile(cfg, s.key.profile_id)
    if route is None or cur is None:
        return route or cur
    return with_settings(route, cur)


def profile_for(st: PersistedState, s: OccurrenceState) -> Profile | None:
    return effective_profile(st.config_snapshots, st.config, s)


def routing_profile(st: PersistedState, pid: str, revision: int | None = None) -> Profile | None:
    regular = find_profile(st.config, pid)
    if regular is not None:
        return regular
    stored = find_profile(st.config_snapshots.get(revision), pid) if revision is not None else None
    if stored is not None and stored.travel_plan:
        return stored
    plan = st.travel_plans.get(pid)
    return plan.profile if plan is not None else None


def commute_day_for(p: Profile, d: Direction, service_day: date) -> date:
    return service_day - timedelta(days=p.journey(d).day_offset)


def baseline_scope(p: Profile, d: Direction, service_day: date) -> int | None:
    return (
        commute_day_for(p, d, service_day).isoweekday()
        if p.journey(d).first_train_by_weekday
        else None
    )


def baseline_on(
    history: BaselineHistory, p: Profile, d: Direction, service_day: date
) -> BaselineVersion | None:
    return history.in_force(service_day, baseline_scope(p, d, service_day))


def route_geometry_same(old: Profile, new: Profile, d: Direction) -> bool:
    a, b = old.journey(d), new.journey(d)
    return (old.origin(d), old.destination(d), a.transfers, a.day_offset) == (
        new.origin(d),
        new.destination(d),
        b.transfers,
        b.day_offset,
    )


def changed_weekdays(old: Profile, new: Profile, d: Direction) -> frozenset[int]:
    monday = date(2026, 10, 26)
    return frozenset(
        index + 1
        for index in range(7)
        if old.journey_on(d, monday + timedelta(days=index)).first_train
        != new.journey_on(d, monday + timedelta(days=index)).first_train
    )


def migrate_baselines(
    old: Profile, new: Profile, d: Direction, history: BaselineHistory, today: date
) -> BaselineHistory:
    """Retain actual learned signatures only where the configured departure is unchanged."""
    a, b = old.journey(d), new.journey(d)
    floor = max(history.floor, history.latest or 0)
    if not route_geometry_same(old, new, d):
        return BaselineHistory((), floor)
    scoped_old, scoped_new = bool(a.first_train_by_weekday), bool(b.first_train_by_weekday)
    if not scoped_old and not scoped_new:
        base = history.in_force(today)
        if a.first_train == b.first_train or (
            base is not None and base.signature.legs[0].departure == b.first_train
        ):
            return history
        return BaselineHistory((), floor)
    if scoped_old and not scoped_new:
        return BaselineHistory((), floor)
    changed = changed_weekdays(old, new, d)
    if scoped_old:
        retained = tuple(v for v in history.versions if v.commute_weekday not in changed)
        return history if retained == history.versions else BaselineHistory(retained, floor)
    versions = []
    retired_floor = floor
    for weekday in range(1, 8):
        if weekday in changed:
            continue
        for version in sorted(history.versions, key=lambda v: v.version):
            if version.commute_weekday is None and version.confirmed:
                floor += 1
                versions.append(replace(version, version=floor, commute_weekday=weekday))
    return BaselineHistory(tuple(versions), retired_floor)


def signature_for(
    st: PersistedState, p: Profile, d: Direction, sd: date, version: int | None = None
) -> Signature | None:
    hist = st.baselines.get(direction_key(p.id, d), BaselineHistory())
    v = next(
        (
            x
            for x in hist.versions
            if x.version == version and x.commute_weekday == baseline_scope(p, d, sd)
        ),
        None,
    ) or baseline_on(hist, p, d, sd)
    n = len(p.journey(d).transfers) + 1
    return v.signature if v is not None and len(v.signature.legs) == n else None


def travel_replaces(st: PersistedState, p: Profile, d: Direction, commute_day: date) -> bool:
    if not st.travel_plans:
        return False
    service_day = commute_day + timedelta(days=p.journey(d).day_offset)
    sig = signature_for(st, p, d, service_day)
    sch = dated_schedule(p, d, commute_day, sig.legs[0].departure if sig else None)
    return plan_suppresses(sch, p, st.travel_plans.values())


def occurrence_ctx(st: PersistedState, s: OccurrenceState, now: datetime) -> Ctx | None:
    p = profile_for(st, s)
    if p is None:
        return None
    d = s.key.direction
    commute_day = s.key.service_date - timedelta(days=p.journey(d).day_offset)
    own = s.baseline_signature
    if own is not None and len(own.legs) == len(p.journey(d).transfers) + 1:
        sig: Signature | None = own
    elif s.baseline_binding is not BaselineBinding.UNHYDRATED:
        sig = None  # it started with no baseline: a later one never moves it (rule 19)
    else:
        sig = signature_for(st, p, d, s.key.service_date, s.baseline_version)
    first = sig.legs[0].departure if sig else None
    schedule = dated_schedule(p, d, commute_day, first)
    if schedule.mode == "window" and p.journey_on(d, commute_day).min_stay_min:
        arrived = stay_from(st, p, d, schedule.first_departure, present(schedule.last_departure))
        schedule = dated_schedule(p, d, commute_day, first, arrived)
    # a window day follows no usual train: a learned one of the fixed days says nothing there
    remembered = frozenset(st.remembered_stations.get(direction_key(p.id, d), ()))
    return Ctx(p, d, schedule, sig if schedule.mode == "fixed" else None, now, remembered)


def stay_from(
    st: PersistedState, p: Profile, d: Direction, start: datetime, end: datetime
) -> datetime | None:
    """The arrival before this window at the other direction's destination: confirmed by the
    user, else first phone presence there, else the accepted train arrival. The newest one in
    the 24 h before the window start up to its latest time."""
    other = Direction.RETURN if d is Direction.OUTBOUND else Direction.OUTBOUND
    lo, hi = to_utc(start) - timedelta(hours=24), to_utc(end)
    found = [
        t
        for r in st.journey_records.values()
        if r.key.profile_id == p.id and r.key.direction is other
        for t in (r.arrived_at or r.seen_at or r.train_arrival,)
        if t is not None and lo < to_utc(t) <= hi
    ]
    return max(found, key=to_utc) if found else None


def frozen(st: PersistedState, s: OccurrenceState) -> OccurrenceState:
    """The occurrence with its own schedule fixed from the history it runs on now, before
    anything replaces that history (rules 10, 19). Unchanged when already fixed or when its
    profile is unknown."""
    if s.baseline_binding is not BaselineBinding.UNHYDRATED:
        return s
    if s.baseline_signature is not None:
        return replace(s, baseline_frozen=True)
    p = profile_for(st, s)
    if p is None:
        return s
    sig = signature_for(st, p, s.key.direction, s.key.service_date, s.baseline_version)
    return replace(s, baseline_signature=sig, baseline_frozen=True)

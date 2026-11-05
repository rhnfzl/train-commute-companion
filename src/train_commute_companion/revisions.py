"""A profile as an occurrence runs it: its route revision with the newest settings, and the schedule
that gives it (7.12 rules 10, 19). Pure: every answer is read from the state it is given."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta

from train_commute_companion.model import (
    Config,
    Ctx,
    Direction,
    Profile,
    Signature,
    UsualJourney,
    direction_key,
)
from train_commute_companion.schedule import dated_schedule
from train_commute_companion.state import BaselineHistory, OccurrenceState
from train_commute_companion.store import PersistedState


def with_settings(route: Profile, settings: Profile) -> Profile:
    """`route`'s trains with `settings`' settings (7.12 rule 10)."""

    def j(d: Direction) -> UsualJourney:
        return replace(
            route.journey(d),
            buffer_min=settings.journey(d).buffer_min,
            earlier_train_advice=settings.journey(d).earlier_train_advice,
        )

    return replace(
        route,
        name=settings.name,
        alerts=settings.alerts,
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


def signature_for(
    st: PersistedState, p: Profile, d: Direction, sd: date, version: int | None = None
) -> Signature | None:
    hist = st.baselines.get(direction_key(p.id, d), BaselineHistory())
    v = next((x for x in hist.versions if x.version == version), None) or hist.in_force(sd)
    n = len(p.journey(d).transfers) + 1
    return v.signature if v is not None and len(v.signature.legs) == n else None


def occurrence_ctx(st: PersistedState, s: OccurrenceState, now: datetime) -> Ctx | None:
    p = profile_for(st, s)
    if p is None:
        return None
    d = s.key.direction
    commute_day = s.key.service_date - timedelta(days=p.journey(d).day_offset)
    own = s.baseline_signature
    if own is not None and len(own.legs) == len(p.journey(d).transfers) + 1:
        sig: Signature | None = own
    elif s.baseline_frozen:
        sig = None  # it started with no baseline: a later one never moves it (rule 19)
    else:
        sig = signature_for(st, p, d, s.key.service_date, s.baseline_version)
    first = sig.legs[0].departure if sig else None
    return Ctx(p, d, dated_schedule(p, d, commute_day, first), sig, now)


def frozen(st: PersistedState, s: OccurrenceState) -> OccurrenceState:
    """The occurrence with its own schedule fixed from the history it runs on now, before
    anything replaces that history (rules 10, 19). Unchanged when already fixed or when its
    profile is unknown."""
    if s.baseline_frozen:
        return s
    if s.baseline_signature is not None:
        return replace(s, baseline_frozen=True)
    p = profile_for(st, s)
    if p is None:
        return s
    sig = signature_for(st, p, s.key.direction, s.key.service_date, s.baseline_version)
    return replace(s, baseline_signature=sig, baseline_frozen=True)

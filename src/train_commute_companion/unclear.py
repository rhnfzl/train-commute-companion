"""The copies of one train in one answer, compared on top of the worst-case merge (7.12 rule 23)."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import quote

from train_commute_companion.calendar import TZ
from train_commute_companion.model import Leg, Provenance, Stop, TrainId
from train_commute_companion.providers.base import ProviderAnswer

Value = tuple[str | None, ...]
Item = tuple[str, str, Value]  # (sid or "leg", field, canonical value)

_WORD = {
    Provenance.REPORTED: "live",
    Provenance.PLANNED: "planned only",
    Provenance.MISSING: "no live time",
}
# compare keys in text order; "plat0" is the first stop's platform, compared apart from a middle
# stop's (same role only) and rendered as "plat"
_ORDER = ("pdep", "dep", "parr", "arr", "canc", "plat0", "plat", "aplat")
_LEG = "leg"


@dataclass(frozen=True, slots=True)
class Unclear:
    fingerprint: str
    label: str
    station: str
    copies: tuple[str, ...]


def train_key(t: TrainId) -> str:
    number = quote(t.train_number, safe="").replace("~", "%7E")
    return f"{number}@{t.planned_departure.astimezone(UTC):%Y%m%dT%H%MZ}"


def newest_copies(answers: Sequence[ProviderAnswer], train: TrainId, board: str) -> list[Leg]:
    """Every copy of `train` boarding at `board` in the newest fetch that holds one."""
    cands = [g for a in answers for c in a.connections for g in c.legs if g.train == train]
    # NS can name the boarding stop by its UIC number in one copy (providers/ns.py `_ids`)
    uics = {g.board.uic for g in cands if g.board.code == board and g.board.uic}
    found = [
        (a.fetched_at.astimezone(UTC), g)
        for a in answers
        for c in a.connections
        for g in c.legs
        if g.train == train and (g.board.code == board or g.board.uic in uics)
    ]
    # shortcut: "one NS answer" is the newest fetch instant, so an older answer of the same poll is not checked and the fake rig's shared instant compares queries together; upgrade if answers ever carry their own id
    top = max((t for t, _ in found), default=None)
    return [g for t, g in found if t == top]


def _instant(t: datetime | None) -> str | None:
    # whole minutes: shortcut: a sub-minute difference is ignored, NS times are whole minutes; upgrade if a provider ever sends seconds
    return None if t is None else t.astimezone(UTC).strftime("%Y%m%dT%H%MZ")


def _clock(t: datetime, day: date) -> str:
    local = t.astimezone(TZ)
    text = f"{local:%H:%M}" + (" (second)" if local.fold else "")
    return text if local.date() == day else f"{text} on {local:%a} {local.day} {local:%b}"


def _utc(*ts: datetime | None) -> list[datetime]:
    return [t.astimezone(UTC) for t in ts if t is not None]


def _earliest(s: Stop) -> datetime | None:
    return min(_utc(s.planned_arrival, s.planned_departure), default=None)


def _planned(s: Stop) -> datetime | None:
    return next(iter(_utc(s.planned_arrival or s.planned_departure)), None)


def _flag(on: bool, yes: str, no: str) -> tuple[Value, str]:
    return (("1",), yes) if on else (("0",), no)


def _state(field: str, s: Stop, day: date) -> tuple[Value, str]:
    """The canonical value of one field of a stop and its text."""
    if field in ("pdep", "parr"):
        t = s.planned_departure if field == "pdep" else s.planned_arrival
        verb, noun = ("leave", "departure") if field == "pdep" else ("arrive", "arrival")
        return (_instant(t),), f"planned to {verb} {_clock(t, day)}" if t else f"no planned {noun}"
    if field in ("dep", "arr"):
        o = s.departure if field == "dep" else s.arrival
        word = "leaves" if field == "dep" else "arrives"
        if o is None:
            return (None, None), f"{word}: no time"
        when = o.value or o.planned
        if when is None:
            return (None, o.provenance.value), f"{word}: no time"
        return (_instant(when), o.provenance.value), (
            f"{word} {_clock(when, day)} ({_WORD[o.provenance]})"
        )
    if field == "canc":
        return _flag(s.cancelled, "stop cancelled", "stop served")
    arriving = field == "aplat"
    planned = (s.planned_arrival_track if arriving else s.planned_track) or None  # "" is no track
    track = (s.actual_arrival_track if arriving else s.actual_track) or planned  # missing: planned
    if track is None:
        text = "no platform"
    elif planned == track:
        text = f"platform {track}"
    elif planned:
        text = f"platform {track} (planned {planned})"
    else:
        text = f"platform {track} (no planned platform)"
    return (planned, track), text


def _platformed(field: str, s: Stop) -> bool:
    """A stop that names a platform on one side only says nothing about the other side."""
    if field not in ("plat0", "plat", "aplat"):
        return True
    dep = bool(s.planned_track or s.actual_track)
    arr = bool(s.planned_arrival_track or s.actual_arrival_track)
    return (arr or not dep) if field == "aplat" else (dep or not arr)


def _bound(copies: Sequence[Leg], keys: list[list[str]], targets: list[str]) -> str | None:
    """The furthest stop where the user gets off, picked by route order (owner decision 6)."""
    listed = [[x for x in k if x in targets] for k in keys]
    if not any(listed):
        return None
    if len(targets) == 1:
        return targets[0]
    best = max(listed, key=lambda xs: len(set(xs)))  # max keeps the first of a tie
    if len(set(best)) >= 2:
        return best[-1]

    def when(x: str) -> datetime:
        ts = [
            _planned(copies[i].stops[j])
            for i, k in enumerate(keys)
            for j, y in enumerate(k)
            if y == x
        ]
        return max((t for t in ts if t is not None), default=datetime.min.replace(tzinfo=UTC))

    return max((x for x in targets if any(x in k for k in keys)), key=when)


def disagreement(copies: Sequence[Leg], alight: str, *also: str) -> Unclear | None:
    """Where copies of one train differ on the user's own trip, up to the furthest of
    `alight` and `also` (where the user gets off); None when they agree."""
    if len(copies) < 2:
        return None
    # one station can be a code in one copy and its UIC number in another (providers/ns.py `_ids`)
    ids: dict[str, str] = {}
    for g in copies:
        for s in g.stops:
            if s.uic:
                ids[s.code] = ids[s.uic] = s.uic
    keys = [[ids.get(s.code, s.code) for s in g.stops] for g in copies]
    targets = list(dict.fromkeys(ids.get(a, a) for a in (alight, *also)))
    bound = _bound(copies, keys, targets)
    holders = [i for i, k in enumerate(keys) if bound in k]
    after = {x for i in holders for x in keys[i][keys[i].index(str(bound)) + 1 :]}
    bound_at = max(
        (t for i in holders if (t := _planned(copies[i].stops[keys[i].index(str(bound))]))),
        default=None,
    )
    boards = {k[0] for k in keys}

    names: dict[str, tuple[bool, str]] = {}  # a code-named stop names the station first
    route: dict[str, datetime] = {}
    stated: dict[tuple[str, str], dict[int, tuple[Value, str]]] = {}
    for i, g in enumerate(copies):
        day = g.train.operating_date
        stated.setdefault((_LEG, "bus"), {})[i] = _flag(
            g.alternative_transport, "bus instead of train", "runs as a train"
        )
        stated.setdefault((_LEG, "canc"), {})[i] = _flag(
            g.cancelled, "train cancelled", "train runs"
        )
        last = len(g.stops) - 1
        cut = keys[i].index(bound) if bound in keys[i] else None
        for j, (x, s) in enumerate(zip(keys[i], g.stops, strict=True)):
            names[x] = min(names.get(x, (True, s.name)), (bool(s.uic) and s.code == s.uic, s.name))
            early = _earliest(s)
            if early is not None:
                route[x] = min(route.get(x, early), early)
            if bound is None:
                part = j == 0  # no bound known: nothing past boarding is risked
            elif cut is not None:
                part = j <= cut  # stop order, never time
            else:  # the copy lacks the bound stop: bounded by planned time
                part = j == 0 or (
                    x not in after
                    and early is not None
                    and bound_at is not None
                    and early <= bound_at
                )
            if not part:
                continue
            fields = ["canc"]
            if j != last and x != bound:  # a leg end gives only its own side
                fields += ["pdep", "dep", "plat0" if j == 0 else "plat"]
            if j != 0 and x not in boards:
                # where the user gets off its arrival platform counts, through stop or not
                fields += ["parr", "arr"] + (["aplat"] if j == last or x == bound else [])
            for f in fields:
                if _platformed(f, s):
                    stated.setdefault((x, f), {})[i] = _state(f, s, day)

    def order(k: tuple[str, str]) -> tuple[bool, datetime, str, int]:
        at = route.get(k[0], datetime.max.replace(tzinfo=UTC))
        field = _ORDER.index(k[1]) if k[0] != _LEG else ("bus", "canc").index(k[1])
        return k[0] != _LEG, at, k[0], field

    differ = sorted(
        (k for k, by in stated.items() if len(by) >= 2 and len({v for v, _ in by.values()}) > 1),
        key=order,
    )
    if not differ:
        return None
    station = next((k[0] for k in differ if k[0] != _LEG), keys[0][0])
    found: dict[tuple[Item, ...], str] = {}
    for i in range(len(copies)):
        mine = [(k, stated[k][i]) for k in differ if i in stated[k]]
        canon = tuple((x, f.rstrip("0"), v) for (x, f), (v, _) in mine)
        texts = [t if x in (_LEG, station) else f"at {names[x][1]} {t}" for (x, _), (_, t) in mine]
        if canon:
            found.setdefault(canon, ", ".join(texts))
    # a copy whose differing values another copy holds too adds nothing
    kept = sorted(
        (c for c in found if not any(set(c) < set(o) for o in found)), key=lambda c: json.dumps(c)
    )
    if len(kept) < 2:
        return None
    cats = [g.category for g in copies if not g.alternative_transport] or [
        g.category for g in copies
    ]
    # keyed by station name: NS may send a stop's UIC number in one poll and not the next
    named = sorted(
        json.dumps([(names[x][1] if x != _LEG else x, f, v) for x, f, v in c]) for c in kept
    )
    return Unclear(
        fingerprint=hashlib.sha256(json.dumps(named).encode()).hexdigest()[:16],
        label=f"{min(cats)} {copies[0].train.train_number}",
        station=names[station][1],
        copies=tuple(found[c] for c in kept),
    )

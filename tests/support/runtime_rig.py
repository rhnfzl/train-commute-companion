"""A Runtime on a fake clock, store, notifier and scripted provider, and the worlds it runs in."""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import runtime_seam as seam
from builders import AMS, make_connection, make_leg, planned_at_alight
from lifecycle_fixtures import L1, L2

from train_commute_companion.model import (
    AlertEvent,
    Connection,
    Direction,
    EventKind,
    Obligation,
    Signature,
    SignatureLeg,
)
from train_commute_companion.providers.base import (
    PlannedWorks,
    ProviderAnswer,
    Query,
)
from train_commute_companion.providers.base import Provider as ProviderPort
from train_commute_companion.runtime import Runtime
from train_commute_companion.serde import dumps
from train_commute_companion.store import VERSION, PersistedState, StorageError, read_state

DAY = date(2026, 10, 27)  # a Tuesday


PROFILE = """
profiles:
  - id: "p1"
    name: "NAME"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "RTD"
    transfer_min: {UT: 4}
    outbound: {first_train: "08:12", buffer_min: 10, transfers: [UT]}
    return: {first_train: "17:52", buffer_min: 5}
  - id: "p2"
    name: "office_b"
    days: [sat]
    home_station: "AAA"
    work_station: "RTD"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()} {hhmm}").replace(tzinfo=AMS)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t = (self.t.astimezone(UTC) + timedelta(seconds=seconds)).astimezone(AMS)

    def jump(self, minutes: float) -> None:
        self.t = (self.t.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(AMS)

    def set(self, hhmm: str) -> None:
        self.t = at(hhmm, self.t.date())


class MemoryStore:
    """Keeps the encoded text, so what survives a restart went through the real codec."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.version = VERSION  # set to an older one: `text` as a file of that version holds it
        self.fail = False
        self.writes = 0

    def load(self) -> PersistedState | None:
        return None if self.text is None else read_state(self.version, json.loads(self.text))

    def save(self, s: PersistedState) -> None:
        if self.fail:
            raise StorageError("disk full")
        self.text = dumps(s)
        self.version = VERSION
        self.writes += 1


class Notifier:
    def __init__(self) -> None:
        self.fails: Callable[[AlertEvent], bool] = lambda e: False
        self.attempts: list[AlertEvent] = []

    async def send(self, event: AlertEvent) -> bool:
        self.attempts.append(event)
        return not self.fails(event)


def pieces(c: Connection, origin: str, destination: str) -> list[Connection]:
    """Every run of whole legs of `c` from `origin` to `destination`."""
    n = len(c.legs)
    return [
        Connection(c.legs[i:j], c.status, None)
        for i in range(n)
        for j in range(i + 1, n + 1)
        if c.legs[i].board.code == origin and c.legs[j - 1].alight.code == destination
    ]


class Provider:
    """Replays a scripted world: every query gets the connections from its origin, fetched now.
    `hook` runs inside a request (to move the clock, act or edit mid-flight); `error` raises.
    `window`: like NS, only connections leaving from the asked time up to that much later."""

    def __init__(self, clock: FakeClock, world: list[Connection]) -> None:
        self.clock = clock
        self.world = world
        self.positions: dict[str, tuple[float, float]] = {}
        self.calls: list[Query] = []
        self.error: Callable[[Query], Exception | None] = lambda q: None
        self.hook: Callable[[Query], object] | None = None
        self.works_calls = 0
        self.window: timedelta | None = None

    async def trips(self, q: Query) -> ProviderAnswer:
        self.calls.append(q)
        if self.hook is not None:
            r = self.hook(q)
            if hasattr(r, "__await__"):
                await r  # type: ignore[misc]
        err = self.error(q)
        if err is not None:
            raise err
        conns = tuple(x for c in self.world for x in pieces(c, q.origin, q.destination))
        if self.window is not None:
            t, w = q.at.astimezone(UTC), self.window
            conns = tuple(c for c in conns if t <= c.legs[0].train.planned_departure < t + w)
        return ProviderAnswer(q, conns, self.clock.now())

    async def planned_works(self) -> PlannedWorks:
        self.works_calls += 1
        return PlannedWorks((), "complete")

    async def stations(self) -> frozenset[str]:
        return frozenset()


class StationsProvider:
    """`inner` with a station list: a real provider has one and `require_stations` wants it."""

    def __init__(
        self,
        inner: ProviderPort,
        codes: frozenset[str],
        positions: Mapping[str, tuple[float, float]] = {},
    ) -> None:
        self.inner, self.codes, self.positions = inner, codes, dict(positions)

    async def trips(self, q: Query) -> ProviderAnswer:
        return await self.inner.trips(q)

    async def planned_works(self) -> PlannedWorks:
        return await self.inner.planned_works()

    async def stations(self) -> frozenset[str]:
        return self.codes


USUAL = make_connection([L1, L2])


def usual_on(day: date, first: str = "08:12") -> Connection:
    return make_connection(
        [
            make_leg("AAA", "UT", first, "08:40", number="1234", day=day),
            make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=day),
        ]
    )


def every_day(first: str = "08:12") -> list[Connection]:
    return [usual_on(DAY + timedelta(days=i), first) for i in range(12)]


class Rig:
    def __init__(self, tmp: Path, now: str = "07:50", name: str = "office_a") -> None:
        self.path = tmp / "profiles.yaml"
        self.write(name)
        self.clock = FakeClock(at(now))
        self.store = MemoryStore()
        self.notifier = Notifier()
        self.provider = Provider(self.clock, [USUAL])
        self.rt = self.new()

    def write(self, name: str = "office_a", text: str | None = None) -> None:
        self.path.write_text(text if text is not None else PROFILE.replace("NAME", name))

    def new(self, *, require_stations: bool = False, on_stations=None) -> Runtime:
        return Runtime(
            profile_path=self.path,
            store=self.store,
            provider=self.provider,
            notifier=self.notifier,
            clock=self.clock,
            require_stations=require_stations,
            on_stations=on_stations,
        )

    async def restart(self) -> Runtime:
        self.rt = self.new()
        await self.rt.start()
        return self.rt

    def occ(self, key: str = "p1|outbound|2026-10-27"):
        return self.rt.state.occurrences.get(key)

    def occ_set(self, s, key: str = "p1|outbound|2026-10-27") -> None:
        st = self.rt.state
        seam.seed_state(self.rt, occurrences={**st.occurrences, key: s})

    async def run_until(self, hhmm: str, poll: bool = True) -> None:
        """Polls every 2 min and runs the watchdog every 30 s up to `hhmm`."""
        end = at(hhmm, self.clock.t.date())
        while self.clock.t < end:
            if poll and self.clock.t.minute % 2 == 0 and self.clock.t.second == 0:
                await self.rt.poll_once()
            await self.rt.watchdog_once()
            await self.clock.sleep(30)


def kinds(events: list[AlertEvent]) -> list[EventKind]:
    return [e.kind for e in events]


async def started(tmp: Path, now: str = "07:50") -> Rig:
    rig = Rig(tmp, now)
    await rig.rt.start()
    await rig.rt.poll_once()
    return rig


PROFILES_DIR = Path(__file__).parents[1] / "data" / "profiles"


_MTIME = [2_000_000_000 * 10**9]


def bump_mtime(path: Path) -> None:
    """A strictly increasing mtime, so two writes in one tick are both seen by check_file."""
    _MTIME[0] += 10**9
    os.utime(path, ns=(_MTIME[0], _MTIME[0]))


OUT = Direction.OUTBOUND


def returns_every_day(first: str) -> list:
    n = f"7{first[3:]}"
    days = [DAY + timedelta(days=i) for i in range(12)]
    return [
        make_connection([make_leg("RTD", "AAA", first, "18:30", number=n, day=d)]) for d in days
    ]


def sig(first: time, second: time) -> Signature:
    return Signature((SignatureLeg("AAA", "UT", first, 0, 20), SignatureLeg("UT", "RTD", second,
                      0, 34)))  # fmt: skip


def tracked(leg, planned: str, actual: str):
    b = replace(leg.stops[0], planned_track=planned, actual_track=actual)
    return replace(leg, stops=(b, *leg.stops[1:]))


def escape_world(escape_cancelled: bool = False) -> list:
    return [
        make_connection([make_leg("RTD", "AAA", "17:22", "18:00", number="7922",
                                  cancelled=escape_cancelled)]),
        make_connection([make_leg("RTD", "AAA", "18:22", "19:00", number="7982")]),
    ]  # fmt: skip


def cancelled_usual_world() -> list:
    return [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952",
                                  cancelled=True)]),
        *escape_world(),
    ]  # fmt: skip


def world_with(day: date, l2: object) -> list:
    """Every usual journey, the outbound on `day` with `l2` as its second leg."""
    l1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=day)
    return [
        *(c for c in every_day() if c.legs[0].train.operating_date != day),
        make_connection([l1, l2]),
        *returns_every_day("17:52"),
    ]


def cancelled_l2(day: date) -> list:
    l2x = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=day, cancelled=True)
    return world_with(day, l2x)


def clean_world() -> list:
    return [*every_day(), *returns_every_day("17:52")]


def split_world(day: date, other) -> list:
    l1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=day)
    l2 = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=day)
    return [
        *(c for c in every_day() if c.legs[0].train.operating_date != day),
        make_connection([planned_at_alight(l1), l2]),
        make_connection([other(l1), l2]),
        *returns_every_day("17:52"),
    ]


def thu_world(leg0: bool, leg1: bool) -> list:
    """Every usual journey, the outbound two days after DAY with leg 0 and leg 1 cancelled or
    not."""
    thu = DAY + timedelta(days=2)
    l1 = make_leg("AAA", "UT", "08:12", "08:40", number="1234", day=thu, cancelled=leg0)
    l2 = make_leg("UT", "RTD", "08:50", "09:24", number="5678", day=thu, cancelled=leg1)
    return [
        *(c for c in every_day() if c.legs[0].train.operating_date != thu),
        make_connection([l1, l2]),
        *returns_every_day("17:52"),
    ]


async def live_facts_bundle(rig: Rig, change: str, stale: bool, fail) -> tuple[Obligation, tuple]:
    """Look-ahead runs on DAY at 05:40, 06:10 (and 06:40) on a rig not started yet; `fail(True)`
    makes every delivery fail from the bundle under test on. "recovery": works on both legs,
    then leg 0 is restored (both when `stale`). "relapse": works on both legs, delivered, both
    back to normal, then leg 1 under works again (both when `stale`). Returns the bundle under
    test, still owed, and the facts of it that still hold."""
    thu = DAY + timedelta(days=2)

    def owed() -> Obligation:
        (o,) = [x for x in rig.rt.state.obligations if x.look_ahead == ("p1", OUT, thu)]
        return o

    rig.provider.world = thu_world(True, True)
    fail(change == "recovery")
    await rig.rt.start()
    await rig.rt.lookahead_once()
    if change == "recovery":
        o = owed()
        rig.clock.set("06:10")
        rig.provider.world = thu_world(False, not stale)
        await rig.rt.lookahead_once()
        keep = f"{thu.isoformat()}|1|warn:"
    else:
        rig.clock.set("06:10")
        rig.provider.world = thu_world(False, False)
        fail(True)
        await rig.rt.lookahead_once()
        o = owed()
        rig.clock.set("06:40")
        rig.provider.world = thu_world(stale, True)
        await rig.rt.lookahead_once()
        keep = f"{thu.isoformat()}|0|restored"
    facts = rig.rt.state.lookahead["p1|outbound"].facts[o.id]
    assert len(facts) > 1  # a bundle
    return o, () if stale else tuple(f for f in facts if f[0].startswith(keep))

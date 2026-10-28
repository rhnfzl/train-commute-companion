"""Durable webhook copies beside the phone obligation (7.12 rules 5, 6, 11, 12, 16, 22; owner
decisions Q1 and Q2): a real WebhookPoster on a MockTransport client, the runtime rig of
tests/test_runtime.py, receivers that verify every delivery with the documented recipe."""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
from builders import AMS, make_connection, make_leg
from test_lifecycle_follow import L1, L1X, L2, LATER
from test_runtime import DAY, PROFILE, MemoryStore, Provider, Rig, at, kinds
from test_runtime_rules import (
    _cancelled_l2,
    _clean,
    cancelled_usual_world,
    live_facts_bundle,
    tracked,
)
from webhook_receiver import Receiver

from train_commute_companion import runtime
from train_commute_companion.delivery import Obligation, WebhookCopy
from train_commute_companion.lifecycle import NotTravelling
from train_commute_companion.lookahead import (
    BaselineHistory,
    BaselineVersion,
    render_message,
    signature_of,
)
from train_commute_companion.model import AlertEvent, Direction, EventKind, Severity
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer, ServerError
from train_commute_companion.runtime import (
    GIVE_UP,
    MAX_COPIES,
    POLL_EVERY_S,
    Runtime,
    WebhookPoster,
    send_webhook_tests,
)
from train_commute_companion.serde import dumps, loads
from train_commute_companion.store import PersistedState
from train_commute_companion.webhook import (
    PROBE_LOUD,
    PROBE_SILENT,
    body,
    event_id,
    probe_id,
    url_id,
)

SECRET = "test-webhook-secret-0001"
TOKEN = "test-webhook-token-0001"
URL = "https://hooks.example.invalid/tcc"
URL2 = "https://hooks2.example.invalid/tcc"
HOST, HOST2 = "hooks.example.invalid", "hooks2.example.invalid"
P1 = PROFILE.split('  - id: "p2"')[0].replace("NAME", "office_a")
OUT = Direction.OUTBOUND
WED, THU = DAY + timedelta(days=1), DAY + timedelta(days=2)


async def _forever():
    while True:
        yield b"x"
        await asyncio.sleep(0.01)


class Endpoint(Receiver):
    """A receiver at one host: answers `status` (with `location`, or a body that never ends),
    or raises `error`; waits for `gate` first when `gate_if` matches the body."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        super().__init__(SECRET, lambda: now().timestamp())
        self._now = now
        self.error: type[httpx.HTTPError] | None = None
        self.location: str | None = None
        self.endless = False
        self.gate: asyncio.Event | None = None
        self.gate_if: Callable[[dict[str, Any]], bool] = lambda b: True
        self.entered = asyncio.Event()
        self.times: list[datetime] = []
        self.accepted: list[tuple[httpx.Request, dict[str, Any]]] = []  # verified, 2xx

    async def ahandler(self, request: httpx.Request) -> httpx.Response:
        self.times.append(self._now())
        if self.gate is not None and self.gate_if(json.loads(request.content)):
            self.entered.set()
            await self.gate.wait()
        if self.error is not None:
            self.requests.append(request)
            raise self.error("down", request=request)
        n = len(self.posts)
        self.handler(request)
        if 200 <= self.status <= 299 and len(self.posts) > n:
            self.accepted.append((request, self.posts[-1]))
        hdrs = {"location": self.location} if self.location else {}
        content = _forever() if self.endless else None
        return httpx.Response(self.status, headers=hdrs, content=content)

    def of(self, kind: EventKind) -> list[dict[str, Any]]:
        return [p for _, p in self.accepted if p["kind"] == kind.value]

    def ids(self, kind: EventKind) -> list[str]:
        return [r.headers["webhook-id"] for r in self.requests if _kind(r) == kind.value]


def _kind(r: httpx.Request) -> str:
    return json.loads(r.content)["kind"]


class RecStore(MemoryStore):
    """Remembers every obligation it ever wrote, so a test can name one after it finished."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: dict[str, Obligation] = {}
        self.copies: list[tuple[WebhookCopy, ...]] = []  # the copies of every write

    def save(self, s: PersistedState) -> None:
        super().save(s)
        self.seen.update({o.id: o for o in s.obligations})
        self.copies.append(s.webhook_copies)


class SyncRuntime(Runtime):
    """Waits for the webhook pass after each delivery, so a test reads its posts at once."""

    async def _deliver(self) -> None:
        await super()._deliver()
        await self.webhooks_idle()


class WRig(Rig):
    """The runtime rig with a WebhookPoster on `urls`, built anew at every restart as a real
    restart does (its rest and outage come back from the store). `sync`: wait for each
    webhook pass (else it runs in its own task, as in production)."""

    def __init__(
        self,
        tmp: Path,
        now: str = "07:50",
        urls: tuple[str, ...] = (URL,),
        hooks: bool = True,
        text: str | None = None,
        sync: bool = True,
    ) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.sync = sync
        self.ends = {h: Endpoint(lambda: self.clock.now()) for h in (HOST, HOST2)}
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: self.ends[r.url.host].ahandler(r))
        )
        self.poster: WebhookPoster | None = (
            WebhookPoster(self.client, urls, SECRET, lambda: self.clock.now()) if hooks else None
        )
        self.end = self.ends[httpx.URL(urls[0]).host]
        super().__init__(tmp, now)
        if text is not None:
            self.write(text=text)
        self.store = RecStore()
        self.rt = self.new()

    def new(self) -> Runtime:
        return (SyncRuntime if self.sync else Runtime)(
            profile_path=self.path,
            store=self.store,
            provider=self.provider,
            notifier=self.notifier,
            clock=self.clock,
            known_stations=None,
            webhooks=self.poster,
        )

    async def restart(self) -> Runtime:
        if self.poster is not None:
            self.poster = WebhookPoster(self.client, self.poster.urls, SECRET, self.clock.now)
        return await super().restart()


async def wstarted(tmp: Path, now: str = "07:50", **kw: Any) -> WRig:
    rig = WRig(tmp, now, **kw)
    await rig.rt.start()
    await rig.rt.poll_once()
    return rig


async def cancel(rig: Rig, hhmm: str = "07:52") -> None:
    rig.clock.set(hhmm)
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()


def cancelled_ob(rig: WRig) -> Obligation:
    (o,) = [o for o in rig.store.seen.values() if o.event.kind is EventKind.CANCELLED]
    return o


def copies(rig: Rig, kind: EventKind | None = None) -> list[WebhookCopy]:
    return [c for c in rig.rt._copies.values() if kind in (None, c.obligation.event.kind)]


def stored_copies(rig: Rig) -> tuple[WebhookCopy, ...]:
    return loads(PersistedState, rig.store.text).webhook_copies


def logged(caplog, level: int, text: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == level and text in r.getMessage()]


async def tick(rig: Rig, seconds: float = 61) -> None:
    rig.clock.jump(seconds / 60)
    await rig.rt.watchdog_once()


def alert(rig: Rig, n: object) -> None:
    """A loud application warning, delivered by the next pass."""
    rig.rt._app_warning(EventKind.KEY_REJECTED, f"alert {n}.", rig.clock.now(), str(n))


# --- delivery to the phone and every URL ---


async def test_alert_goes_to_phone_and_every_url_signed(tmp_path):
    rig = await wstarted(tmp_path, urls=(URL, URL2))
    await cancel(rig)
    (phone,) = [e for e in rig.rt.events_log if e.kind is EventKind.CANCELLED]
    o = cancelled_ob(rig)
    for ep in rig.ends.values():
        assert ep.of(EventKind.CANCELLED) == [json.loads(body(phone))]
        assert ep.ids(EventKind.CANCELLED) == [event_id(o.id)]
        assert ep.bad == 0
    assert not any(x.id == o.id for x in rig.rt.state.obligations)
    assert rig.rt._copies == {} and stored_copies(rig) == ()


async def test_phone_alone_counts_as_delivered(tmp_path):
    rig = await wstarted(tmp_path / "a")
    rig.end.status = 500
    await cancel(rig)
    assert rig.rt.view().undelivered == 0
    assert kinds(rig.rt.events_log).count(EventKind.CANCELLED) == 1
    assert copies(rig, EventKind.CANCELLED)
    rig = await wstarted(tmp_path / "b")
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    await cancel(rig)
    assert rig.rt.view().undelivered == 1
    for _ in range(3):
        await tick(rig, 30)
    assert len(rig.end.of(EventKind.CANCELLED)) == 1


async def test_phone_retries_do_not_repost_an_accepted_webhook(tmp_path):  # Review Focus 2
    rig = await wstarted(tmp_path)
    tries = iter(range(100))
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED and next(tries) < 5
    await cancel(rig)
    for _ in range(5):
        await tick(rig, 30)
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 6
    assert kinds(rig.rt.events_log).count(EventKind.CANCELLED) == 1
    assert len(rig.end.ids(EventKind.CANCELLED)) == 1


async def test_dead_webhook_never_changes_delivery_and_backs_off(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="train_commute_companion")  # Review Focus 1
    rig = await wstarted(tmp_path, "20:00")
    rig.end.error = httpx.ReadTimeout
    for i in range(5):  # 5 alerts within 60 s
        alert(rig, i)
        await rig.rt.watchdog_once()
        rig.clock.jump(10 / 60)
    assert rig.rt.view().undelivered == 0
    assert kinds(rig.rt.events_log).count(EventKind.KEY_REJECTED) == 5
    assert len(rig.end.requests) == 1 and rig.poster.failing
    tried = [c.obligation.event.text for c in copies(rig) if c.first_try is not None]
    assert len(rig.rt._copies) == 5 and tried == ["App: alert 0."]
    (w,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert w.getMessage() == f"webhook to {HOST} failed (ReadTimeout); next try after 60 s"
    rig.clock.t = rig.end.times[0] + timedelta(seconds=61)
    await rig.rt.watchdog_once()
    assert len(rig.end.requests) == 2 and json.loads(rig.end.requests[1].content)["text"] == (
        "App: alert 1."
    )
    assert sum(c.first_try is not None for c in copies(rig)) == 2
    rig.end.error = None
    await tick(rig)
    order = [p["text"] for _, p in rig.end.accepted]
    assert order == [f"App: alert {i}." for i in (2, 3, 4, 0, 1)]
    assert len(logged(caplog, logging.INFO, "works again")) == 1
    assert not rig.poster.failing and rig.rt._copies == {}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_continuous_outage_shares_tries_and_bounds_the_queue(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="train_commute_companion")  # Q1, Review Focus 1
    rig = await wstarted(tmp_path, "20:00")
    rig.end.status = 503
    made: dict[str, datetime] = {}
    ahead: dict[str, int] = {}
    first: dict[str, datetime] = {}
    last_seen: dict[str, datetime] = {}
    longest = 0
    for step in range(6 * 120):  # 6 h, the watchdog every 30 s
        if step == 360:
            await rig.restart()
        if step % 20 == 0:  # a new alert every 10 min
            alert(rig, step)
        waiting = sum(c.first_try is None for c in copies(rig))
        await rig.rt.watchdog_once()
        now = rig.clock.now()
        for c in copies(rig):
            made.setdefault(c.id, now)
            ahead.setdefault(c.id, waiting)
            last_seen[c.id] = now
            if c.first_try is not None:
                first.setdefault(c.id, c.first_try)
        longest = max(longest, len(rig.rt._copies))
        await rig.clock.sleep(30)
    for i, t in made.items():
        assert first[i] - t <= timedelta(seconds=61) * (ahead[i] + 1)
        assert last_seen[i] - first[i] < GIVE_UP + timedelta(seconds=30)
    assert longest <= 14
    gave = logged(caplog, logging.WARNING, "gave up a webhook copy after 2 h")
    assert len(gave) == len(made) - len(rig.rt._copies) > 0
    times = rig.end.times
    assert all(b - a >= timedelta(seconds=60) for a, b in pairwise(times))


async def test_alerts_faster_than_the_rest_hit_the_cap(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="train_commute_companion")  # round 5
    rig = await wstarted(tmp_path, "20:00")
    rig.end.status = 503
    dropped = gave = 0
    for step in range(6 * 120):  # a new alert every 30 s for 6 h
        before = dict(rig.rt._copies)
        if step == 360:
            await rig.restart()
        alert(rig, step)
        await rig.rt.watchdog_once()
        now, after = rig.clock.now(), rig.rt._copies
        assert len(after) <= MAX_COPIES
        assert len(json.loads(rig.store.text)["webhook_copies"]) <= MAX_COPIES
        never = [c for c in before.values() if c.first_try is None]
        for cid in before.keys() - after.keys():
            c = before[cid]
            if c.first_try is None:  # the cap: the oldest never-tried copy, never a tried one
                assert c.obligation.event.created_at == min(
                    x.obligation.event.created_at for x in never
                )
                dropped += 1
            else:
                assert GIVE_UP <= now - c.first_try < GIVE_UP + timedelta(seconds=30)
                gave += 1
        assert all(now - c.first_try < GIVE_UP for c in after.values() if c.first_try)
        await rig.clock.sleep(30)
    assert len(logged(caplog, logging.WARNING, "dropped a webhook copy: more than 200")) == dropped
    assert len(logged(caplog, logging.WARNING, "gave up")) == gave
    assert dropped > 0 and gave > 0
    times = rig.end.times
    assert all(b - a >= timedelta(seconds=60) for a, b in pairwise(times))


@pytest.mark.parametrize("wait", ["observation", "reconcile"])
@pytest.mark.parametrize(
    "kind", ["ALL_GOOD", "LEAVE_NOW", "EARLIER_TRAIN_WITHDRAWN", "NEXT_CONNECTION"]
)
async def test_give_up_holds_through_observation_waits(tmp_path, kind, wait, caplog):  # Q1
    caplog.set_level(logging.WARNING, logger="train_commute_companion")
    rig = await wstarted(tmp_path)
    t0 = rig.clock.now()
    e = AlertEvent(
        "p1", "office_a", OUT, DAY, None, EventKind[kind], Severity.SILENT, L1.train,
        "Office A: seeded.", f"{kind}:seeded", t0,
    )  # fmt: skip
    o = replace(rig.rt._obligation(e, rig.occ(), t0), webhook_copied=True)  # sent by phone
    st = loads(PersistedState, rig.store.text)
    rig.store.text = dumps(
        replace(st, webhook_copies=(WebhookCopy(event_id(o.id), o, (), t0, t0),))
    )
    rig.end.status = 503
    rig.end.requests.clear()
    rig.provider.error = lambda q: ServerError()  # no healthy board answer after the restart
    if wait == "observation":
        rig.clock.t = t0 + GIVE_UP - timedelta(minutes=1)
        await rig.restart()
        assert event_id(o.id) in rig.rt._copies and rig.rt._boards == {}  # waits to observe
        rig.clock.t = t0 + GIVE_UP + timedelta(seconds=30)
        await rig.rt.watchdog_once()
    else:
        rig.clock.t = t0 + GIVE_UP + timedelta(seconds=30)
        await rig.restart()  # ends inside the reconcile wait of start()
    cid = event_id(o.id)  # (the stale board's own cannot-see alert gets a copy of its own)
    assert cid not in rig.rt._copies and cid not in {x.id for x in stored_copies(rig)}
    assert cid not in [r.headers["webhook-id"] for r in rig.end.requests]
    assert len(logged(caplog, logging.WARNING, "gave up a webhook copy after 2 h")) == 1


# --- an owed copy is judged like its phone obligation (Review Focus 3, 4) ---


@pytest.mark.parametrize("case", ["leave_now_expired", "skipped", "renamed", "told_cancellation"])
async def test_owed_copy_is_revalidated_before_each_try(tmp_path, case):
    if case == "leave_now_expired":
        rig = WRig(tmp_path, "16:54")
        rig.provider.world = cancelled_usual_world()
        rig.end.status = 503
        await rig.rt.start()  # LEAVE_NOW for the 17:22 told by phone; its copy is owed
        assert copies(rig, EventKind.LEAVE_NOW)
        rig.clock.set("17:20")  # past leaving time for the 17:22
        await rig.rt.poll_once()
        rig.end.status = 200
        await tick(rig)
        assert rig.end.of(EventKind.LEAVE_NOW) == []
        return
    rig = await wstarted(tmp_path)
    rig.end.status = 503
    await cancel(rig)
    assert copies(rig, EventKind.CANCELLED)
    if case == "skipped":
        await rig.rt.act("p1", NotTravelling(None, False))
    elif case == "renamed":
        rig.write("office_x")
        await rig.rt.reload()
    rig.end.status = 200
    await tick(rig)
    await tick(rig)
    got = rig.end.of(EventKind.CANCELLED)
    if case == "skipped":
        assert got == []
    elif case == "renamed":
        assert len(got) == 1 and got[0]["text"].startswith("Office X:")
        assert got[0]["profile_name"] == "office_x"
    else:
        assert len(got) == 1


async def test_owed_copy_survives_a_restart_without_a_phone_resend(tmp_path):  # Review Focus 4
    rig = await wstarted(tmp_path)
    rig.end.status = 503
    await cancel(rig)
    (first,) = rig.end.ids(EventKind.CANCELLED)
    await rig.restart()
    rig.end.status = 200
    await tick(rig)
    assert len(rig.end.of(EventKind.CANCELLED)) == 1
    assert set(rig.end.ids(EventKind.CANCELLED)) == {first}
    assert kinds(rig.notifier.attempts).count(EventKind.CANCELLED) == 1


@pytest.mark.parametrize("recover", ["after", "at_escalation", "after_restart"])
async def test_escalation_supersedes_the_queued_silent_copy(tmp_path, recover):  # rule 5(b)
    rig = await wstarted(tmp_path)
    rig.end.error = httpx.ReadTimeout
    for hhmm in ("07:52", "07:58"):  # silent, then loud within 15 min of the 08:12
        if hhmm == "07:58" and recover == "at_escalation":
            rig.end.error = None
        rig.clock.set(hhmm)
        rig.provider.world = [make_connection([tracked(L1, "1", "5"), L2])]
        await rig.rt.poll_once()
    pc = [e.severity for e in rig.rt.events_log if e.kind is EventKind.PLATFORM_CHANGE]
    assert pc == [Severity.SILENT, Severity.LOUD]
    if recover == "after_restart":
        await rig.restart()
    rig.end.error = None
    await tick(rig)
    await tick(rig)
    got = [p["severity"] for p in rig.end.of(EventKind.PLATFORM_CHANGE)]
    assert got == [Severity.LOUD.value]
    assert copies(rig, EventKind.PLATFORM_CHANGE) == []


@pytest.mark.parametrize("restart", [False, True])
async def test_a_newer_cancellation_never_ends_the_queued_one(tmp_path, restart):  # rule 5(a)
    rig = await wstarted(tmp_path)
    rig.end.error = httpx.ReadTimeout
    for hhmm, world in (("07:52", [make_connection([L1X, L2]), LATER]),
                        ("07:54", [make_connection([L1, L2]), LATER]),
                        ("07:56", [make_connection([L1X, L2]), LATER])):  # fmt: skip
        rig.clock.set(hhmm)
        rig.provider.world = world
        await rig.rt.poll_once()
    assert kinds(rig.rt.events_log).count(EventKind.CANCELLED) == 2
    if restart:
        await rig.restart()
    rig.end.error = None
    await tick(rig)
    await tick(rig)
    assert len(rig.end.of(EventKind.CANCELLED)) == 2
    assert copies(rig, EventKind.CANCELLED) == []


async def test_quiet_hours_hold_the_copy_and_the_give_up_counts_from_the_first_try(
    tmp_path, caplog
):
    caplog.set_level(logging.WARNING, logger="train_commute_companion")
    quiet = P1.replace("    transfer_min:", '    alerts: {quiet_hours: ["19:52", "07:12"]}\n'
                       "    transfer_min:")  # fmt: skip
    rig = WRig(tmp_path, "23:40", text=quiet)
    await rig.rt.start()
    rig.end.status = 503
    e = AlertEvent(
        "p1", "office_a", OUT, WED, None, EventKind.DELAYED, Severity.SILENT, None,
        "Office A: seeded.", "delayed:seeded", rig.clock.now(),
    )  # fmt: skip
    rt = rig.rt
    rt._st = replace(
        rt._st, obligations=(*rt._st.obligations, rt._obligation(e, None, e.created_at))
    )
    await rt.watchdog_once()
    while rig.clock.now() < at("07:10", WED):
        await tick(rig, 30 * 60)
    assert rig.notifier.attempts == [] and rig.end.requests == []
    (c,) = copies(rig)
    assert c.first_try is None  # held: the give-up clock has not started
    rig.clock.t = at("07:12", WED)
    await rt.watchdog_once()  # quiet hours over: both go out
    assert [x.key for x in rig.notifier.attempts] == ["delayed:seeded"]
    assert len(rig.end.requests) == 1 and copies(rig)[0].first_try == at("07:12", WED)
    while rig.clock.now() < at("09:11:30", WED):
        await tick(rig, 30)
    assert copies(rig) and not logged(caplog, logging.WARNING, "gave up")  # never sooner
    rig.clock.t = at("09:12", WED)
    await rt.watchdog_once()
    assert copies(rig) == []
    assert len(logged(caplog, logging.WARNING, "gave up a webhook copy after 2 h")) == 1


async def test_two_profiles_on_one_train_get_two_posts(tmp_path):  # Review Focus 7
    rig = await wstarted(tmp_path)
    p2_only = PROFILE.split('  - id: "p2"')[1].replace("days: [sat]", "days: [tue]")
    rig.write(text='profiles:\n  - id: "p2"' + p2_only)
    rig.clock.jump(1)
    await rig.rt.check_file()
    rig.clock.set("07:52")
    await rig.rt.poll_once()
    rig.notifier.fails = lambda e: True
    rig.end.status = 503
    rig.clock.set("07:54")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    await rig.rt.poll_once()
    owed = [o for o in rig.rt.state.obligations if o.event.kind is EventKind.CANCELLED]
    assert sorted(o.event.profile_id for o in owed) == ["p1", "p2"]
    rig.end.status = 200
    await tick(rig)
    assert sorted(p["profile_id"] for p in rig.end.of(EventKind.CANCELLED)) == ["p1", "p2"]
    assert sorted(rig.end.ids(EventKind.CANCELLED)[-2:]) == sorted(event_id(o.id) for o in owed)


async def test_copies_survive_failed_writes_and_held_phone_success(tmp_path):  # rule 22
    rig = await wstarted(tmp_path)
    rig.end.status = 503
    rig.store.fail = True
    text = rig.store.text
    await cancel(rig)  # the cancellation is held in memory; the phone took it
    (c,) = copies(rig, EventKind.CANCELLED)
    assert c.id in rig.rt._held_copies
    assert not any(o.event.kind is EventKind.CANCELLED for o in rig.rt._held.values())
    rig.end.status = 200
    await tick(rig)
    assert len(rig.end.of(EventKind.CANCELLED)) == 1 and copies(rig, EventKind.CANCELLED) == []
    alert(rig, "second")  # made and posted while every write fails
    await rig.rt._deliver()  # (a watchdog's retried write would reset the unwritten alert)
    assert len(rig.end.of(EventKind.KEY_REJECTED)) == 1 and rig.store.text == text
    rig.end.status = 503
    alert(rig, "third")
    await rig.rt._deliver()
    assert [x.obligation.event.text for x in copies(rig, EventKind.KEY_REJECTED)] == [
        "App: alert third."
    ]
    rig.store.fail = False
    await rig.rt.watchdog_once()
    assert stored_copies(rig) == tuple(rig.rt._copies.values()) != ()
    phone = len(rig.notifier.attempts)
    await rig.restart()
    rig.end.status = 200
    await tick(rig)
    assert len(rig.end.of(EventKind.CANCELLED)) == 1
    assert [p["text"] for p in rig.end.of(EventKind.KEY_REJECTED)] == [
        "App: alert second.",
        "App: alert third.",
    ]
    assert len(rig.notifier.attempts) == phone and rig.rt._copies == {}


async def test_a_pass_cancelled_during_a_post_keeps_the_new_copy(tmp_path):  # rule 22
    rig = await wstarted(tmp_path, "20:00")
    rig.end.gate = asyncio.Event()
    alert(rig, 1)
    task = asyncio.create_task(rig.rt.watchdog_once())
    await asyncio.wait_for(rig.end.entered.wait(), 2)
    await rig.rt._stop_posting()  # shutdown cuts the POST off
    await task
    (c,) = stored_copies(rig)
    assert c.obligation.event.text == "App: alert 1." and rig.rt._copies == {c.id: c}
    assert c.first_try == rig.clock.now()  # the cut-off try counts (owner decision Q1)
    assert not loads(PersistedState, rig.store.text).obligations  # the phone took it


# --- look-ahead copies (rules 5, 18) ---


@pytest.mark.parametrize("works", ["gone", "unchanged"])
async def test_look_ahead_copy_keeps_its_facts_after_the_phone_send(tmp_path, works):
    rig = WRig(tmp_path, "05:40", text=P1)
    rig.provider.world = _cancelled_l2(THU)
    rig.end.status = 503
    await rig.rt.start()
    await rig.rt.lookahead_once()  # a silent works warning, told by phone
    (c,) = copies(rig, EventKind.WORKS_AHEAD)
    assert c.obligation.event.severity is Severity.SILENT
    assert not rig.rt.state.lookahead["p1|outbound"].pending
    rig.clock.set("06:10")
    rig.provider.world = _clean() if works == "gone" else _cancelled_l2(THU)
    await rig.rt.lookahead_once()
    assert c.obligation.id in rig.rt.state.lookahead["p1|outbound"].facts
    rig.end.status = 200
    await tick(rig)
    assert len(rig.end.of(EventKind.WORKS_AHEAD)) == (0 if works == "gone" else 1)
    assert copies(rig, EventKind.WORKS_AHEAD) == []
    rig.clock.set("06:40")
    await rig.rt.lookahead_once()
    assert c.obligation.id not in rig.rt.state.lookahead["p1|outbound"].facts


async def test_skip_after_window_close_ends_journey_warning_copies(tmp_path):
    rig = await wstarted(tmp_path)
    rig.end.status = 503
    await cancel(rig)
    (c,) = copies(rig, EventKind.CANCELLED)
    await rig.rt.act("p1", NotTravelling(None, False))
    st = rig.store.load()  # the skip and the removal in one write
    assert st.occurrences["p1|outbound|2026-10-27"].not_travelling
    assert c.id not in {x.id for x in st.webhook_copies}
    rig.clock.set("11:00")
    await rig.rt.watchdog_once()
    assert "p1|outbound|2026-10-27" not in rig.rt.state.occurrences  # archived
    await rig.restart()
    rig.end.status = 200
    await tick(rig)
    assert rig.end.of(EventKind.CANCELLED) == []


@pytest.mark.parametrize("whole_day", [False, True])
async def test_skip_during_an_in_flight_post_is_never_undone(tmp_path, whole_day):
    rig = await wstarted(tmp_path)
    rig.end.gate = asyncio.Event()
    rig.end.gate_if = lambda b: b["kind"] == "cancelled"
    task = asyncio.create_task(cancel(rig))
    await asyncio.wait_for(rig.end.entered.wait(), 2)
    skip = asyncio.create_task(rig.rt.act("p1", NotTravelling(None, whole_day)))
    for _ in range(20):
        await asyncio.sleep(0)
    assert copies(rig, EventKind.CANCELLED) == []
    assert not any(x.obligation.event.kind is EventKind.CANCELLED for x in stored_copies(rig))
    skipped_at = len(rig.store.copies)
    rig.end.status = 503
    rig.end.gate.set()
    await task
    assert await skip
    assert copies(rig, EventKind.CANCELLED) == []
    for written in rig.store.copies[skipped_at:]:  # never restored, not even for a moment
        assert not any(x.obligation.event.kind is EventKind.CANCELLED for x in written)
    rig.end.gate = None
    rig.clock.set("23:00")
    await rig.rt.watchdog_once()
    assert "p1|outbound|2026-10-27" not in rig.rt.state.occurrences
    await rig.restart()
    rig.end.status = 200
    await tick(rig)
    assert rig.end.of(EventKind.CANCELLED) == []


# the bundled adoption and works warning of tests/test_lookahead.py, on the runtime: (leg 1 of
# the whole journey, leg 2 of it, leg 2 asked alone)
BUNDLE = (("08:15", "08:43"), ("08:49", "09:27"), ("08:55", "09:33"))
CONTINUE = (("08:12", "08:40"), ("08:46", "09:24"), ("08:55", "09:33"))  # adoption withdrawn
GONE = (("08:12", "08:40"), ("08:46", "09:24"), ("08:46", "09:24"))  # and the works ended
WARNING = "your UT to RTD leg changes to 08:55"


class Mixed(Provider):
    times = BUNDLE

    async def trips(self, q):
        self.calls.append(q)
        day = q.at.astimezone(AMS).date()
        (a1, a2), (b1, b2), (c1, c2) = self.times
        a = make_leg("AAA", "UT", a1, a2, number="1000", day=day)
        b = make_leg("UT", "RTD", b1, b2, number="2000", day=day)
        alone = make_leg("UT", "RTD", c1, c2, number="2002", day=day)
        back = make_leg("RTD", "AAA", "17:52", "18:30", number="7952", day=day)
        legs = {
            ("AAA", "UT"): [[a]],
            ("UT", "RTD"): [[alone]],
            ("AAA", "RTD"): [[a, b]],
            ("RTD", "AAA"): [[back]],
        }.get((q.origin, q.destination), [])
        return ProviderAnswer(q, tuple(make_connection(x) for x in legs), self.clock.now())


async def mixed_rig(tmp: Path, urls: tuple[str, ...] = (URL,), sync: bool = True) -> WRig:
    """05:40 on DAY, a confirmed baseline at 08:12; the next look-ahead run tells tomorrow's
    adoption bundled with a works warning."""
    rig = WRig(tmp, "05:40", urls=urls, text=P1, sync=sync)
    rig.provider = Mixed(rig.clock, [])
    rig.rt = rig.new()
    await rig.rt.start()
    a = make_leg("AAA", "UT", "08:12", "08:40", number="1000")
    b = make_leg("UT", "RTD", "08:46", "09:24", number="2000")
    v1 = BaselineVersion(1, signature_of(make_connection([a, b]), DAY), date(2026, 10, 1), True)
    rig.rt._st = replace(rig.rt._st, baselines={"p1|outbound": BaselineHistory((v1,))})
    rig.rt._save()
    return rig


def bundle_of(rig: WRig) -> Obligation:
    (o,) = [
        o
        for o in rig.store.seen.values()
        if o.look_ahead == ("p1", OUT, WED) and "adopt:" in o.fact_key
    ]
    assert WARNING in o.event.text and "NS moved your" in o.event.text
    return o


async def withdraw(rig: WRig, times=CONTINUE) -> None:
    """The next look-ahead run, without its delivery pass: it withdraws the adoption."""
    rig.clock.jump(5)
    rig.provider.times = times
    await rig.rt._lookahead(rig.rt.config.profiles[0], OUT, PlannedWorks((), "complete"))


def wed_posts(ep: Endpoint) -> list[tuple[str, str]]:
    return [
        (r.headers["webhook-id"], p["text"])
        for r, p in ep.accepted
        if "Wednesday 28 Oct" in p["text"]
    ]


@pytest.mark.parametrize("variant", ["works_continue", "works_gone", "phone_pending"])
async def test_withdrawn_adoption_keeps_the_owed_warning_copy(tmp_path, variant):  # rules 18, 5
    rig = await mixed_rig(tmp_path)
    rig.end.status = 503
    rig.notifier.fails = lambda e: variant == "phone_pending"
    await rig.rt.lookahead_once()
    o = bundle_of(rig)
    cid = event_id(o.id)
    rig.notifier.attempts.clear()
    rig.clock.jump(5)
    rig.provider.times = GONE if variant == "works_gone" else CONTINUE
    await rig.rt.lookahead_once()  # withdraws the adoption, then a delivery pass
    told = [e.text for e in rig.notifier.attempts]
    if variant != "phone_pending":
        assert not any(WARNING in t for t in told)  # no repeat on the phone
        assert any("ignore the earlier change" in t for t in told)  # the silent correction
    if variant == "works_continue":
        c = rig.rt._copies[cid]  # the same webhook-id, its warning without the adoption
        assert WARNING in c.obligation.event.text and "NS moved your" not in c.obligation.event.text
    elif variant == "works_gone":
        assert cid not in rig.rt._copies  # its warning no longer holds: dropped at its try
    await rig.restart()
    rig.end.status = 200
    rig.notifier.fails = lambda e: False
    await tick(rig)
    posts = wed_posts(rig.end)
    warned = [(i, t) for i, t in posts if WARNING in t]
    if variant == "works_continue":
        assert len(warned) == 1 and warned[0][0] == cid and "NS moved your" not in warned[0][1]
    elif variant == "works_gone":
        assert warned == []
    else:
        assert len(warned) == 1 and "NS moved your" not in warned[0][1]
    if variant != "phone_pending":
        assert len([t for _, t in posts if "ignore the earlier change" in t]) == 1


@pytest.mark.parametrize("two", [False, True])
async def test_rewrite_during_an_accepted_first_post_ends_the_copy(tmp_path, two):  # round 5
    rig = await mixed_rig(tmp_path, urls=(URL, URL2) if two else (URL,))
    gate = asyncio.Event()
    for ep in rig.ends.values():
        ep.gate, ep.gate_if = gate, lambda b: "NS moved your" in b["text"]
    first = asyncio.create_task(rig.rt.lookahead_once())
    await asyncio.wait_for(rig.end.entered.wait(), 2)
    o = bundle_of(rig)
    c = rig.rt._copies[event_id(o.id)]
    t0 = rig.clock.now()
    assert c.first_try == t0 and c.last_try is None  # its first POST is in flight
    rig.clock.jump(5)
    rig.provider.times = CONTINUE
    second = asyncio.create_task(rig.rt.lookahead_once())
    for _ in range(200):
        await asyncio.sleep(0)
        if rig.rt._copies.get(c.id) is not c:
            break
    assert "NS moved your" not in rig.rt._copies[c.id].obligation.event.text  # rewritten
    rig.ends[HOST2].status = 503
    gate.set()
    await first
    await second
    if not two:
        assert c.id not in rig.rt._copies and c.id not in {x.id for x in stored_copies(rig)}
        return
    cur = rig.rt._copies[c.id]
    assert cur.done == (url_id(URL),) and cur.first_try == cur.last_try == t0
    assert "NS moved your" not in cur.obligation.event.text
    assert stored_copies(rig) == tuple(rig.rt._copies.values())


@pytest.mark.parametrize("receivers", ["all_accepted", "one_of_two_accepted", "all_failed"])
async def test_retired_phone_pending_bundle_keeps_its_webhook_history(tmp_path, receivers, caplog):
    caplog.set_level(logging.WARNING, logger="train_commute_companion")  # Review Focus 12
    rig = await mixed_rig(tmp_path, urls=(URL, URL2))
    a, b = rig.ends[HOST], rig.ends[HOST2]
    a.status = 503 if receivers == "all_failed" else 200
    b.status = 200 if receivers == "all_accepted" else 503
    rig.notifier.fails = lambda e: True  # the bundle stays phone-pending
    await rig.rt.lookahead_once()
    o = bundle_of(rig)
    cid = event_id(o.id)
    before = rig.rt._copies.get(cid)
    await withdraw(rig)  # retire carries the warning into a new message
    (n,) = [
        x
        for x in rig.rt.state.obligations
        if x.look_ahead == ("p1", OUT, WED) and WARNING in x.event.text
    ]
    assert n.id != o.id and "NS moved your" not in n.event.text
    await rig.restart()
    if receivers == "all_accepted":
        assert before is None and n.webhook_copied
        assert not any(c.obligation.id == n.id for c in copies(rig))
    else:
        c = rig.rt._copies[cid]  # the same copy: webhook-id, receipts and first try kept
        assert c.obligation.id == n.id and c.first_try == before.first_try
        assert c.done == before.done == ((url_id(URL),) if receivers != "all_failed" else ())
    rig.notifier.fails = lambda e: False
    if receivers == "one_of_two_accepted":
        b.status = 200
    await tick(rig)
    assert [e.key for e in rig.rt.events_log].count(n.event.key) == 1  # the phone, once
    for ep in (a, b):
        got = [(i, t) for i, t in wed_posts(ep) if WARNING in t]
        if receivers == "all_failed":
            assert got == []
        elif ep is a or receivers == "all_accepted":
            assert len(got) == 1 and "NS moved your" in got[0][1]  # the bundle, never again
        else:
            assert got == [(cid, n.event.text)]  # only the carried warning
    if receivers == "all_failed":
        rig.clock.t = before.first_try + GIVE_UP
        await rig.rt.watchdog_once()
        assert cid not in rig.rt._copies
        assert len(logged(caplog, logging.WARNING, "gave up")) >= 1


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("change", ["relapse", "recovery"])
async def test_partial_relapse_or_recovery_sends_only_live_facts(tmp_path, change, stale, restart):
    rig = WRig(tmp_path, "05:40")  # Review Focus 12, rule 5(b)

    def fail(down: bool) -> None:
        rig.notifier.fails = lambda e: down
        rig.end.status = 503 if down else 200

    o, live = await live_facts_bundle(rig, change, stale, fail)
    tried = [
        r.headers["webhook-id"]
        for r in rig.end.requests
        if json.loads(r.content)["key"] == o.event.key
    ]
    assert tried and set(tried) == {event_id(o.id)}
    if restart:
        await rig.restart()
    fail(False)
    rig.notifier.attempts.clear()
    await tick(rig)
    phone = [(e.kind.value, e.text) for e in rig.notifier.attempts if e.key == o.event.key]
    hook = [
        (r.headers["webhook-id"], p["kind"], p["text"])
        for r, p in rig.end.accepted
        if p["key"] == o.event.key
    ]
    if stale:
        assert phone == [] and hook == []
        return
    kind, text = render_message(rig.rt.config.profiles[0], o.look_ahead[2], live)
    assert phone == [(kind.value, text)]
    assert hook == [(event_id(o.id), kind.value, text)]


async def test_start_without_webhooks_drops_saved_copies(tmp_path, caplog):  # Review Focus 13
    caplog.set_level(logging.INFO, logger="train_commute_companion")
    rig = await wstarted(tmp_path, "20:00")
    rig.end.status = 503
    alert(rig, 1)
    await rig.rt.watchdog_once()
    alert(rig, 2)
    await rig.rt.watchdog_once()
    assert len(stored_copies(rig)) == 2
    poster, rig.poster = rig.poster, None
    await rig.restart()
    assert len(logged(caplog, logging.INFO, "dropped 2 webhook copies: webhooks are off")) == 1
    assert rig.rt._copies == {} and stored_copies(rig) == ()
    alert(rig, 3)
    await rig.rt.watchdog_once()
    assert kinds(rig.rt.events_log) == [EventKind.KEY_REJECTED]
    assert not any(o.webhook_copied for o in rig.store.seen.values() if "alert 3" in o.event.text)
    rig.poster = poster
    await rig.restart()
    rig.end.status = 200
    await tick(rig)
    assert rig.end.accepted == []


async def test_rename_keeps_the_snapshot_an_owed_copy_needs(tmp_path):  # rule 5(c)
    rig = WRig(tmp_path, "05:40")
    rig.provider.world = _cancelled_l2(THU)
    rig.end.status = 503
    await rig.rt.start()
    await rig.rt.lookahead_once()
    (c,) = [x for x in copies(rig, EventKind.WORKS_AHEAD) if x.obligation.event.profile_id == "p1"]
    assert not rig.rt.state.lookahead["p1|outbound"].pending  # finished on the phone
    rig.write("office_x")
    rig.clock.jump(1)
    await rig.rt.check_file()
    assert rig.rt.config.revision == 2
    await rig.restart()
    assert c.obligation.route_revision in rig.store.load().config_snapshots
    rig.end.status = 200
    await tick(rig)
    got = [p for p in rig.end.of(EventKind.WORKS_AHEAD) if p["profile_id"] == "p1"]
    assert len(got) == 1 and got[0]["text"].startswith("Office X, ")


async def test_store_never_holds_a_webhook_url(tmp_path):
    secret_url = "https://hooks.example.invalid/tcc/route-token-123"
    rig = await wstarted(tmp_path, "20:00", urls=(secret_url, URL2))
    for ep in rig.ends.values():
        ep.status = 503
    alert(rig, 1)
    await rig.rt.watchdog_once()
    assert "route-token-123" not in rig.store.text and HOST not in rig.store.text
    rig.ends[HOST].status = 200
    await tick(rig)
    (c,) = stored_copies(rig)
    assert c.done == (url_id(secret_url),)
    assert "route-token-123" not in rig.store.text and HOST not in rig.store.text


async def test_stalled_or_endless_answer_is_cut_at_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "TRY_DEADLINE_S", 0.05)  # Review Focus 1
    rig = await wstarted(tmp_path, "20:00")
    rig.end.gate = asyncio.Event()  # never answers
    alert(rig, 1)
    await asyncio.wait_for(rig.rt.watchdog_once(), 2)
    assert kinds(rig.rt.events_log) == [EventKind.KEY_REJECTED] and rig.poster.failing
    alert(rig, 2)
    await rig.rt.watchdog_once()
    assert len(rig.rt.events_log) == 2 and len(rig.end.times) == 1  # resting
    rig.end.gate, rig.end.endless = None, True  # a 200 whose body never ends
    await asyncio.wait_for(tick(rig), 2)
    assert len(rig.end.of(EventKind.KEY_REJECTED)) == 2 and rig.rt._copies == {}


@pytest.mark.parametrize(
    ("status", "location"),
    [(302, URL2), (302, "/elsewhere"), (401, None), (404, None)],
)
async def test_redirect_and_client_errors_are_not_followed(tmp_path, caplog, status, location):
    caplog.set_level(logging.WARNING, logger="train_commute_companion")  # Review Focus 6
    rig = await wstarted(tmp_path, "20:00")
    rig.end.status, rig.end.location = status, location
    alert(rig, 1)
    await rig.rt.watchdog_once()
    assert len(rig.end.times) == 1 and rig.ends[HOST2].times == []
    assert kinds(rig.rt.events_log) == [EventKind.KEY_REJECTED]
    (w,) = logged(caplog, logging.WARNING, "webhook to")
    assert f"({status})" in w.getMessage()
    await tick(rig)
    assert len(rig.end.times) == 2


async def test_secret_and_url_never_logged_through_httpcore(caplog):  # Review Focus 5
    for name in ("train_commute_companion", "httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
    heads: list[str] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            except asyncio.IncompleteReadError:
                break
            m = re.search(r"(?i)content-length: (\d+)", head)
            await reader.readexactly(int(m.group(1)) if m else 0)
            heads.append(head)
            if len(heads) == 1:
                out = "HTTP/1.1 302 Found\r\nlocation: /tcc/route-token-123\r\n"
            else:
                skip = ("content-length", "host", "connection", "transfer-encoding")
                lines = [x for x in head.split("\r\n")[1:] if x and not x.lower().startswith(skip)]
                out = "HTTP/1.1 200 OK\r\n" + "".join(x + "\r\n" for x in lines)
            writer.write((out + "content-length: 0\r\n\r\n").encode("latin-1"))
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/tcc/route-token-123"
    clock = [datetime(2026, 10, 27, 20, 0, tzinfo=AMS)]
    e = AlertEvent(
        "p1", "office_a", OUT, DAY, None, EventKind.CANCELLED, Severity.LOUD, None,
        "Office A: cancelled.", "cancelled:x", clock[0],
    )  # fmt: skip
    async with httpx.AsyncClient() as client:
        poster = WebhookPoster(client, [url], SECRET, lambda: clock[0], token=TOKEN)
        assert await poster.post(e, "tcc_x", [url]) == (frozenset({url}), frozenset())  # 302
        clock[0] += timedelta(seconds=61)
        assert await poster.post(e, "tcc_x", [url]) == (frozenset({url}), frozenset({url}))
        async with httpx.AsyncClient() as other:  # outside a webhook try: still logged
            await other.get(f"http://127.0.0.1:{port}/plain")
        # the options-flow test send runs this same path on hass.loop
        (res,) = await send_webhook_tests(client, [url], SECRET, lambda: clock[0], token=TOKEN)
        assert res.ok
        server.close()
        await server.wait_closed()
        clock[0] += timedelta(seconds=61)
        assert await poster.post(e, "tcc_x", [url]) == (frozenset({url}), frozenset())
    assert len(heads) == 5 and all("v1," in h and TOKEN in h for h in heads[3:])  # all there
    assert "v1," in heads[1] and TOKEN in heads[1]
    texts = [r.getMessage() + (r.exc_text or "") for r in caplog.records]
    for bad in (SECRET, TOKEN, "route-token-123", "v1,"):
        assert not any(bad in t for t in texts), bad
    assert any("webhook to 127.0.0.1 failed (302)" in t for t in texts)
    assert any("webhook to 127.0.0.1 failed (ConnectError)" in t for t in texts)
    assert any("/plain" in t for t in texts)


async def test_stopped_posts_nothing(tmp_path):
    rig = WRig(tmp_path, "20:00")
    rig.poster = WebhookPoster(rig.client, (URL,), SECRET, rig.clock.now, stopped=lambda: True)
    rig.rt = rig.new()
    await rig.rt.start()
    alert(rig, 1)
    await rig.rt.watchdog_once()
    assert kinds(rig.rt.events_log) == [EventKind.KEY_REJECTED]
    assert rig.end.times == []
    (c,) = copies(rig)
    assert c.first_try is None


async def test_webhook_test_sends_are_signed_marked_and_never_stored(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)  # Review Focus 11
    rig = await wstarted(tmp_path, "20:00", urls=(URL, URL2))
    a, b = rig.ends[HOST], rig.ends[HOST2]
    b.status = 401
    res = await send_webhook_tests(rig.client, [URL, URL2], SECRET, rig.clock.now, token=TOKEN)
    assert [r.host for r in res] == [HOST, HOST2]
    assert res[0].ok and not res[1].ok and (res[1].loud, res[1].silent) == (401, 401)
    t = rig.clock.now()
    assert [(p["severity"], p["text"], p["test"]) for p in a.posts] == [
        ("loud", PROBE_LOUD, True),
        ("silent", PROBE_SILENT, True),
    ]
    ids = [r.headers["webhook-id"] for r in a.requests]
    assert ids == [probe_id(Severity.LOUD, t), probe_id(Severity.SILENT, t)] and len(set(ids)) == 2
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in a.requests)
    b.error = httpx.ReadTimeout
    (r,) = await send_webhook_tests(rig.client, [URL2], SECRET, rig.clock.now)
    assert (r.loud, r.silent) == ("ReadTimeout", "ReadTimeout")
    monkeypatch.setattr(runtime, "TRY_DEADLINE_S", 0.05)
    b.error, b.gate = None, asyncio.Event()  # never answers
    (r,) = await send_webhook_tests(rig.client, [URL2], SECRET, rig.clock.now)
    assert (r.loud, r.silent) == ("TimeoutError", "TimeoutError")
    b.gate = None
    a.status = 503  # a URL resting in the runtime's poster, failing
    e = AlertEvent(
        "", "app", None, None, None, EventKind.KEY_REJECTED, Severity.LOUD, None, "App: x.",
        "x", t,
    )  # fmt: skip
    await rig.poster.post(e, "tcc_x", [URL])
    rest = dict(rig.poster.down)
    assert rig.poster.failing and URL in rest
    a.status = 200
    caplog.clear()
    res = await send_webhook_tests(rig.client, [URL, URL2], SECRET, rig.clock.now, token=TOKEN)
    assert res[0].ok and rig.poster.failing and rig.poster.down == rest
    await rig.rt.watchdog_once()
    assert stored_copies(rig) == () and rig.rt._copies == {}
    mine = [r.getMessage() for r in caplog.records if r.name == "train_commute_companion.runtime"]
    assert mine == [
        f"webhook test to {HOST}: accepted",
        f"webhook test to {HOST2}: not accepted (loud 401, silent 401)",
    ]
    for rec in caplog.records:
        text = rec.getMessage()
        assert SECRET not in text and TOKEN not in text and "/tcc" not in text


async def test_without_webhooks_the_state_is_as_today(tmp_path):
    rig = await wstarted(tmp_path / "a", hooks=False)
    await cancel(rig)
    (tmp_path / "b").mkdir()
    plain = Rig(tmp_path / "b")
    await plain.rt.start()
    await plain.rt.poll_once()
    await cancel(plain)
    assert rig.rt._copies == {} and not any(o.webhook_copied for o in rig.store.seen.values())
    assert stored_copies(rig) == ()
    assert kinds(rig.rt.events_log) == kinds(plain.rt.events_log)
    assert [e.text for e in rig.rt.events_log] == [e.text for e in plain.rt.events_log]


# --- codex group review: backlog, failed look-ahead writes, cut-off tries, restarts ---


async def test_urgent_advice_reaches_the_phone_while_a_backlog_drains(tmp_path):
    rig = WRig(tmp_path, "16:54", sync=False)  # the webhook pass in its own task, as in production
    rig.provider.window = timedelta(hours=1)
    rig.end.gate = asyncio.Event()  # a slow receiver: every backlog POST waits for it
    rig.end.gate_if = lambda b: b["kind"] == "key_rejected"
    await rig.rt.start()
    for i in range(100):
        alert(rig, i)
    await asyncio.wait_for(rig.rt.watchdog_once(), 2)
    await asyncio.wait_for(rig.end.entered.wait(), 2)  # the first backlog POST is in flight
    rig.provider.world = cancelled_usual_world()
    await asyncio.wait_for(rig.rt.poll_once(), 2)  # never waits behind the receiver
    leave = [e for e in rig.rt.events_log if e.kind is EventKind.LEAVE_NOW]
    assert len(leave) == 1 and "17:22" in leave[0].text and rig.end.accepted == []
    await asyncio.wait_for(rig.rt.watchdog_once(), 2)  # the watchdog neither
    rig.end.gate.set()
    await asyncio.wait_for(rig.rt.webhooks_idle(), 5)
    assert len(rig.end.of(EventKind.KEY_REJECTED)) == 100
    assert len(rig.end.of(EventKind.LEAVE_NOW)) == 1 and rig.rt._copies == {}


@pytest.mark.parametrize("branch", ["rewritten", "carried"])
async def test_failed_look_ahead_write_keeps_the_changed_copy(tmp_path, branch):  # rule 22
    rig = await mixed_rig(tmp_path, urls=(URL, URL2))
    a, b = rig.ends[HOST], rig.ends[HOST2]
    if branch == "carried":  # the bundle stays phone-pending; one receiver took it
        rig.notifier.fails = lambda e: True
        b.status = 503
    else:  # the phone took the bundle; no receiver did
        a.status = b.status = 503
    await rig.rt.lookahead_once()
    o = bundle_of(rig)
    cid = event_id(o.id)
    assert cid in rig.rt._copies
    rig.store.fail = True
    await withdraw(rig)  # the run's write fails
    c = rig.rt._copies[cid]
    assert WARNING in c.obligation.event.text and "NS moved your" not in c.obligation.event.text
    assert cid in rig.rt._held_copies  # judged on what the failed write tried, not the old state
    a.status = b.status = 200
    rig.notifier.fails = lambda e: False
    await tick(rig)  # judged on what the failed write tried: posted, never dropped
    got = [(i, t) for i, t in wed_posts(b) if WARNING in t]
    assert got == [(cid, c.obligation.event.text)] and cid not in rig.rt._copies
    rig.store.fail = False
    await tick(rig)
    assert [(i, t) for i, t in wed_posts(b) if WARNING in t] == got  # once


async def test_carried_bundle_keeps_an_acceptance_the_store_missed(tmp_path):  # rule 22
    rig = await mixed_rig(tmp_path)
    rig.notifier.fails = lambda e: True  # the bundle stays phone-pending
    await rig.rt._lookahead(rig.rt.config.profiles[0], OUT, PlannedWorks((), "complete"))
    rig.store.fail = True
    await rig.rt._deliver()  # the receiver takes the bundle; the delivery write fails
    o = bundle_of(rig)
    assert [i for i, _ in wed_posts(rig.end)] == [event_id(o.id)] and rig.rt._copies == {}
    rig.store.fail = False
    await withdraw(rig)  # retire carries the warning into a new message
    (n,) = [
        x
        for x in rig.rt.state.obligations
        if x.look_ahead == ("p1", OUT, WED) and WARNING in x.event.text
    ]
    assert n.id != o.id and n.webhook_copied
    rig.notifier.fails = lambda e: False
    await tick(rig)
    assert [e.key for e in rig.rt.events_log].count(n.event.key) == 1  # the phone, once
    assert [i for i, _ in wed_posts(rig.end)] == [event_id(o.id)]  # the receiver, never again


async def test_background_write_keeps_the_mark_of_an_in_flight_phone_send(tmp_path):  # rule 22
    rig = await mixed_rig(tmp_path, sync=False)
    phone = asyncio.Event()
    entered = asyncio.Event()
    send = rig.notifier.send

    async def held(e: AlertEvent) -> bool:  # the bundle's phone send waits, then fails
        if WARNING in e.text and not phone.is_set():
            entered.set()
            await phone.wait()
            return False
        return await send(e)

    rig.notifier.send = held  # type: ignore[method-assign]
    rig.end.gate = asyncio.Event()
    rig.end.gate_if = lambda b: b["kind"] == "key_rejected"
    alert(rig, 1)
    await rig.rt.watchdog_once()
    await asyncio.wait_for(rig.end.entered.wait(), 2)  # a webhook pass is in flight
    await rig.rt.watchdog_once()  # queues a second round of it
    run = asyncio.create_task(rig.rt.lookahead_once())
    await asyncio.wait_for(entered.wait(), 2)  # the bundle's copy is made, its phone send waits
    o = bundle_of(rig)
    rig.end.gate.set()
    # the queued round takes the bundle and writes while the phone send waits
    await asyncio.wait_for(rig.rt.webhooks_idle(), 2)
    assert [i for i, _ in wed_posts(rig.end)] == [event_id(o.id)] and rig.rt._copies == {}
    await withdraw(rig)  # retire carries the warning while the phone send is in flight
    phone.set()
    await run
    await rig.rt.webhooks_idle()
    (n,) = [
        x
        for x in rig.rt.state.obligations
        if x.look_ahead == ("p1", OUT, WED) and WARNING in x.event.text
    ]
    assert n.id != o.id and n.webhook_copied
    await tick(rig)
    await rig.rt.webhooks_idle()
    assert [e.key for e in rig.rt.events_log].count(n.event.key) == 1  # the phone, once
    assert [i for i, _ in wed_posts(rig.end)] == [event_id(o.id)]  # the receiver, never again


async def test_background_write_keeps_the_mark_of_a_held_in_flight_phone_send(tmp_path):
    # rule 22: the bundle is held after a failed write; a background write lands meanwhile
    rig = await mixed_rig(tmp_path, sync=False)
    phone = asyncio.Event()
    entered = asyncio.Event()
    send = rig.notifier.send

    async def held(e: AlertEvent) -> bool:  # the bundle's phone send waits, then fails
        if WARNING in e.text and not phone.is_set():
            entered.set()
            await phone.wait()
            return False
        return await send(e)

    rig.notifier.send = held  # type: ignore[method-assign]
    rig.end.gate = asyncio.Event()
    rig.end.gate_if = lambda b: b["kind"] == "key_rejected"
    alert(rig, 1)
    await rig.rt.watchdog_once()
    await asyncio.wait_for(rig.end.entered.wait(), 2)  # a webhook pass is in flight
    await rig.rt.watchdog_once()  # queues a second round of it
    rig.store.fail = True
    run = asyncio.create_task(rig.rt.lookahead_once())
    await asyncio.wait_for(entered.wait(), 2)  # the bundle is held, its copy made
    (o,) = [
        x
        for x in rig.rt._held.values()
        if x.look_ahead == ("p1", OUT, WED) and "adopt:" in x.fact_key
    ]
    rig.store.fail = False
    rig.end.gate.set()
    # the queued round takes the bundle and its write lands while the phone send waits
    await asyncio.wait_for(rig.rt.webhooks_idle(), 2)
    assert [i for i, _ in wed_posts(rig.end)] == [event_id(o.id)] and rig.rt._copies == {}
    assert rig.store.seen[o.id].webhook_copied  # written with its mark: a restart posts none
    await withdraw(rig)  # retire carries the warning while the phone send is in flight
    phone.set()
    await run
    await rig.rt.webhooks_idle()
    (n,) = [
        x
        for x in rig.rt.state.obligations
        if x.look_ahead == ("p1", OUT, WED) and WARNING in x.event.text
    ]
    assert n.id != o.id and n.webhook_copied
    await tick(rig)
    await rig.rt.webhooks_idle()
    assert [e.key for e in rig.rt.events_log].count(n.event.key) == 1  # the phone, once
    assert [i for i, _ in wed_posts(rig.end)] == [event_id(o.id)]  # the receiver, never again


async def test_a_try_cut_off_by_shutdown_still_starts_the_two_hours(tmp_path, caplog):  # Q1
    caplog.set_level(logging.WARNING, logger="train_commute_companion")
    rig = await wstarted(tmp_path, "20:00")
    rig.end.gate = asyncio.Event()
    alert(rig, 1)
    task = asyncio.create_task(rig.rt.watchdog_once())
    await asyncio.wait_for(rig.end.entered.wait(), 2)
    await rig.rt._stop_posting()  # shutdown during the first POST
    await task
    rig.end.gate = None
    rig.clock.set("22:01")
    await rig.restart()
    assert rig.end.of(EventKind.KEY_REJECTED) == [] and rig.rt._copies == {}
    assert len(rig.end.times) == 1
    assert len(logged(caplog, logging.WARNING, "gave up a webhook copy after 2 h")) == 1


async def test_restart_keeps_the_rest_and_the_outage(tmp_path, caplog):  # rule 22
    caplog.set_level(logging.WARNING, logger="train_commute_companion")
    rig = await wstarted(tmp_path, "20:00")
    rig.end.status = 503
    alert(rig, 1)
    await rig.rt.watchdog_once()
    assert len(rig.end.times) == 1 and rig.poster.failing
    old = rig.poster
    rig.clock.jump(1 / 60)
    await rig.restart()  # a new poster, as a real restart has
    assert rig.poster is not old and rig.poster.failing  # failing comes back from the store
    alert(rig, 2)
    await rig.rt.watchdog_once()
    assert len(rig.end.times) == 1  # still resting: no request
    rig.clock.t = rig.end.times[0] + timedelta(seconds=61)
    await rig.rt.watchdog_once()
    assert len(rig.end.times) == 2
    assert len(logged(caplog, logging.WARNING, "webhook to")) == 1  # one warning per outage
    rig.end.status = 200
    await tick(rig)
    assert not rig.poster.failing and loads(PersistedState, rig.store.text).webhook_down == {}


async def test_run_forever_posts_the_copy_between_steps_without_a_wait(tmp_path):
    # production's SystemClock.sleep is a real asyncio.sleep, so the background pass runs
    # while the loops wait; here time runs 200x faster, as real sleeps on a real loop clock,
    # and no driver awaits webhooks_idle()
    rig = await wstarted(tmp_path, sync=False)
    rig.clock.set("07:52")
    rig.provider.world = [make_connection([L1X, L2]), LATER]
    loop, start, t0, fast = asyncio.get_running_loop(), 0.0, rig.clock.t, 200
    polls: list[int] = []  # the receiver's post count at the start of every poll step
    poll = rig.rt.poll_once

    async def counted() -> None:
        polls.append(len(rig.end.of(EventKind.CANCELLED)))
        if len(polls) > 2:
            raise asyncio.CancelledError
        await poll()

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds / fast)

    rig.clock.now = lambda: t0 + timedelta(seconds=(loop.time() - start) * fast)  # type: ignore[method-assign]
    rig.clock.sleep = sleep  # type: ignore[method-assign]
    rig.rt.poll_once = counted  # type: ignore[method-assign]
    start = loop.time()
    with pytest.raises(asyncio.CancelledError):
        await rig.rt.run_forever(started=True)
    (phone,) = [e for e in rig.rt.events_log if e.kind is EventKind.CANCELLED]
    assert rig.end.of(EventKind.CANCELLED) == [json.loads(body(phone))]
    assert polls == [0, 1, 1]  # posted after the poll that made it, before the next one
    assert rig.end.times[-1] - phone.created_at < timedelta(seconds=POLL_EVERY_S)
    assert rig.end.bad == 0

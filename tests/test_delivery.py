import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from builders import AMS

from train_commute_companion.delivery import (
    Obligation,
    WebhookCopy,
    deliver,
    pending_journey_warnings,
    post_copy,
    revalidate,
    zone_report_stops_silent,
)
from train_commute_companion.model import (
    AlertEvent,
    Direction,
    EventKind,
    OccurrenceKey,
    Severity,
)
from train_commute_companion.webhook import event_id, url_id

NOW = datetime(2026, 10, 27, 8, 0, tzinfo=AMS)
KEY = OccurrenceKey("p", Direction.OUTBOUND, date(2026, 10, 27))


def ob(kind: EventKind, sev: Severity = Severity.LOUD, **kw: object) -> Obligation:
    e = AlertEvent(
        "p", "p", Direction.OUTBOUND, KEY.service_date, None, kind, sev, None, "t", "k", NOW
    )
    base = Obligation("1", e, KEY, None, 1, None, "f", None, 0)
    return replace(base, **kw)


@dataclass
class View:
    skip: bool = False
    holds: bool = True
    covered: bool = False
    live: bool = True
    quiet: bool = False

    def skipped(self, key: OccurrenceKey) -> bool:
        return self.skip

    def fact_holds(self, o: Obligation) -> bool:
        return self.holds

    def newer_covers(self, o: Obligation) -> bool:
        return self.covered

    def revision_live(self, key: OccurrenceKey, rev: int) -> bool:
        return self.live

    def in_quiet_hours(self, profile_id: str, now: datetime) -> bool:
        return self.quiet

    def current(self, o: Obligation) -> AlertEvent:
        return o.event


def test_cancellation_told_after_lock_moved_to_good_replacement():
    assert revalidate(ob(EventKind.CANCELLED), View(holds=False, live=False), NOW) == "send"


def test_not_travelling_drops_cancelled_but_key_rejected_still_sent():
    v = View(skip=True)
    assert revalidate(ob(EventKind.CANCELLED), v, NOW) == "drop"
    assert revalidate(ob(EventKind.KEY_REJECTED), v, NOW) == "send"


def test_cancelled_waiting_adoption_message_dropped():
    o = ob(EventKind.TIMETABLE_ADOPTED, Severity.SILENT)
    assert revalidate(o, View(holds=False), NOW) == "drop"
    assert revalidate(ob(EventKind.LEAVE_NOW, expires_at=NOW), View(), NOW) == "drop"
    assert revalidate(o, View(covered=True), NOW) == "drop"


def test_silent_held_in_quiet_hours_but_all_good_not():
    v = View(quiet=True)
    assert revalidate(ob(EventKind.DELAYED, Severity.SILENT), v, NOW) == "hold"
    assert revalidate(ob(EventKind.ALL_GOOD, Severity.SILENT), v, NOW) == "send"
    assert revalidate(ob(EventKind.LEAVE_NOW), v, NOW) == "send"


def test_route_edit_drops_obligation_unless_occurrence_on_that_revision():
    o = ob(EventKind.DELAYED, Severity.SILENT)
    assert revalidate(o, View(live=False), NOW) == "drop"
    assert revalidate(o, View(live=True), NOW) == "send"
    assert revalidate(ob(EventKind.STORAGE_FAILED), View(live=False), NOW) == "send"


class Flaky:
    def __init__(self, results: list[bool]) -> None:
        self.results, self.sent = results, 0

    async def send(self, event: AlertEvent) -> bool:
        self.sent += 1
        return self.results.pop(0)


def test_failed_stopped_following_is_retried():
    o = ob(EventKind.STOPPED_FOLLOWING)
    n = Flaky([False, True])
    kept = asyncio.run(deliver(o, View(holds=False), n, NOW))
    assert kept is not None and kept.attempts == 1
    assert asyncio.run(deliver(kept, View(holds=False), n, NOW + timedelta(minutes=2))) is None
    assert n.sent == 2


def test_pending_count_counts_only_this_occurrences_journey_warnings():
    other = OccurrenceKey("p", Direction.RETURN, KEY.service_date)
    obs = [
        ob(EventKind.CANCELLED),
        ob(EventKind.DELAYED, Severity.SILENT),
        ob(EventKind.MISSING, occurrence=other),
    ]
    assert pending_journey_warnings(obs, KEY) == 1


def test_destination_zone_report_stops_repeat_silent_messages():
    from test_lifecycle_end import arrived, report

    assert not zone_report_stops_silent(arrived(), {"RTD", "work"})
    assert zone_report_stops_silent(report(arrived(), "work", "09:31"), {"RTD", "work"})


def test_zone_report_helper_as_fact_holds_drops_silent_through_revalidate():
    from test_lifecycle_end import arrived, report

    zones = {"RTD", "work"}

    @dataclass
    class ZoneView(View):
        state: object = None

        def fact_holds(self, o: Obligation) -> bool:
            return not zone_report_stops_silent(self.state, zones)  # type: ignore[arg-type]

    o = ob(EventKind.DELAYED, Severity.SILENT)
    assert revalidate(o, ZoneView(state=arrived()), NOW) == "send"
    assert revalidate(o, ZoneView(state=report(arrived(), "work", "09:31")), NOW) == "drop"


def test_look_ahead_obligation_skips_revision_check_and_relies_on_fact_holds():
    # spec 7.12 rule 5(b): look-ahead facts die with their revision/baseline via fact_holds
    o = ob(
        EventKind.DELAYED,
        Severity.SILENT,
        occurrence=None,
        look_ahead=("p", Direction.OUTBOUND, KEY.service_date),
    )
    assert revalidate(o, View(live=False), NOW) == "send"
    assert revalidate(o, View(live=False, holds=False), NOW) == "drop"


def test_send_regenerates_the_message_from_current_state():  # rule 5
    frozen = ob(EventKind.CANCELLED)
    now_text = "Office A: your train is cancelled. Next good connection: 09:05."

    @dataclass
    class Current(View):
        def current(self, o: Obligation) -> AlertEvent:
            return replace(o.event, text=now_text)  # the 08:44 named earlier has left

    class Record:
        def __init__(self) -> None:
            self.sent: list[AlertEvent] = []

        async def send(self, event: AlertEvent) -> bool:
            self.sent.append(event)
            return False

    n = Record()
    kept = asyncio.run(deliver(frozen, Current(), n, NOW))
    assert [e.text for e in n.sent] == [now_text]
    assert kept is not None and kept.event == frozen.event  # the stored fact stays as written


class Port:
    """A stub webhook port: `accept` take it; `rest` are tried by nobody."""

    def __init__(self, urls: tuple[str, ...], accept: set[str], rest: set[str] | None = None):
        self.urls, self.accept, self.rest = urls, accept, rest or set()
        self.calls: list[tuple[AlertEvent, str, tuple[str, ...]]] = []

    async def post(self, event, msg_id, urls):
        self.calls.append((event, msg_id, tuple(urls)))
        tried = frozenset(u for u in urls if u not in self.rest)
        return tried, frozenset(u for u in tried if u in self.accept)


A, B = "https://hooks.example.invalid/tcc", "https://hooks2.example.invalid/tcc"


async def test_post_copy_follows_revalidation():
    o = ob(EventKind.DELAYED, Severity.SILENT)
    c = WebhookCopy(event_id(o.id), o)
    port = Port((A, B), {A})
    assert await post_copy(c, View(holds=False), port, NOW) is None  # drop
    assert await post_copy(c, View(quiet=True), port, NOW) is c  # hold
    assert port.calls == []
    now_text = "Office A: still late."

    @dataclass
    class Current(View):
        def current(self, o: Obligation) -> AlertEvent:
            return replace(o.event, text=now_text)

    r = await post_copy(c, Current(), port, NOW)
    assert [(e.text, i, us) for e, i, us in port.calls] == [(now_text, event_id(o.id), (A, B))]
    assert r is not None and r.done == (url_id(A),) and r.first_try == r.last_try == NOW
    later = NOW + timedelta(minutes=1)
    port.accept = set()
    r2 = await post_copy(r, View(), port, later)  # only B is owed; it fails
    assert port.calls[-1][2] == (B,)
    assert r2 is not None and r2.first_try == NOW and r2.last_try == later
    port.accept = {B}
    assert await post_copy(r2, View(), port, later) is None  # every URL took it
    assert await post_copy(c, View(), Port((A, B), {A, B}), NOW) is None
    resting = Port((A, B), {A, B}, rest={A, B})
    same = await post_copy(c, View(), resting, NOW)  # nothing tried: the clock does not start
    assert same is c and same.first_try is None and same.last_try is None

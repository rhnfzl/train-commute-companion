import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from builders import AMS

from train_commute_companion.delivery import (
    Obligation,
    deliver,
    pending_journey_warnings,
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

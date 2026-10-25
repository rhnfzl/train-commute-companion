from dataclasses import replace

from builders import make_connection, make_leg
from test_lifecycle_follow import L1, L2, at, board, ctx, journey, occ

from train_commute_companion.classify import classify_leg
from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.model import (
    EventKind,
    Provenance,
    Severity,
    TransferCheck,
    TransferState,
)
from train_commute_companion.news import alert_key, all_good, escalations, severity, unsent
from train_commute_companion.providers.base import ProviderAnswer, Query

LOCK = journey([L1, L2])
OK_T = TransferCheck(TransferState.OK, 10, 4, "ok")


def key(leg1=L1, transfer=OK_T, target=None) -> str:
    cls = [classify_leg(leg1, "UT"), classify_leg(L2, "RTD")]
    return alert_key(LOCK, cls, [transfer], target)


def sev(kind, minutes=None, **k):
    d = {"breaks_transfer": False, "past_latest_arrival": False, "is_tomorrow": False} | k
    return severity(kind, minutes_to_departure=minutes, **d)


def test_unmapped_kind_is_loud():
    assert sev(EventKind.OFFLINE) is Severity.LOUD
    assert sev(EventKind.CUT_SHORT) is Severity.LOUD
    assert sev(EventKind.ALL_GOOD) is Severity.SILENT


def test_transfer_at_risk_silent_then_loud_inside_30_min_once():  # rule 4
    assert sev(EventKind.TRANSFER_AT_RISK, 45) is Severity.SILENT
    assert sev(EventKind.TRANSFER_AT_RISK, 30) is Severity.LOUD
    facts = [("risk:k", EventKind.TRANSFER_AT_RISK)]
    ledger = {"risk:k": Severity.SILENT}
    assert escalations(ledger, facts, ctx(at("07:12"))) == []  # first train 08:12, 60 min out
    assert escalations(ledger, facts, ctx(at("07:50"))) == ["risk:k"]
    assert escalations({"risk:k": Severity.LOUD}, facts, ctx(at("07:50"))) == []


def test_platform_change_loud_within_15_min():
    assert sev(EventKind.PLATFORM_CHANGE, 16) is Severity.SILENT
    assert sev(EventKind.PLATFORM_CHANGE, 15) is Severity.LOUD


def test_delayed_and_works_ahead_are_silent_unless_they_matter():
    assert sev(EventKind.DELAYED) is Severity.SILENT
    assert sev(EventKind.DELAYED, breaks_transfer=True) is Severity.LOUD
    assert sev(EventKind.DELAYED, past_latest_arrival=True) is Severity.LOUD
    assert sev(EventKind.WORKS_AHEAD) is Severity.SILENT
    assert sev(EventKind.WORKS_AHEAD, is_tomorrow=True) is Severity.LOUD


def test_delay_steps_of_3_and_back_to_normal_at_1():
    base = key()
    assert key(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=1)) == base
    two = key(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=2))
    four = key(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=5))
    three = key(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=3))
    assert two != base and three == four  # inside one step, no news
    assert key(make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=6)) != three


def test_key_changes_with_transfer_state_and_relock_target():
    assert key(transfer=TransferCheck(TransferState.AT_RISK, 5, 4, "x")) != key()
    assert key(target=L2.train) != key()


def test_repeated_missing_and_retained_events_sent_once():
    c = ctx(at("08:00"))
    from train_commute_companion.lifecycle import _event

    def evs():
        return [
            _event(c, EventKind.MISSING, Severity.LOUD, "x"),
            replace(_event(c, EventKind.MISSING, Severity.LOUD, "y"), key="missing:retained:77"),
        ]

    ledger: dict[str, Severity] = {}
    first = unsent(ledger, evs())
    assert len(first) == 2
    ledger.update({e.key: e.severity for e in first})
    assert unsent(ledger, evs()) == []


# all_good


def good_board(s: OccurrenceState, *legs, polled="08:00"):
    q = Query("AAA", "RTD", at("08:00"))
    from builders import make_connection

    a = ProviderAnswer(q, (make_connection(legs),), q.at)
    return replace(board(s), answers=(a,))


def test_all_good_once_when_all_reported_and_ok():  # spec fixture
    s = occ(LOCK)
    e = all_good(s, good_board(s, L1, L2), ctx(at("08:02")))
    assert e is not None and e.kind is EventKind.ALL_GOOD and e.severity is Severity.SILENT
    assert e.text.startswith("Office A: your 08:12 to RTD is on time per NS, change at UT")
    assert (
        all_good(replace(s, all_good_evaluated=True), good_board(s, L1, L2), ctx(at("08:02")))
        is None
    )


def test_no_all_good_when_one_leg_stale_or_planned_or_transfer_at_risk():
    s = occ(LOCK)
    c = ctx(at("08:02"))
    assert all_good(s, good_board(s, L1, L2), ctx(at("08:30"))) is None  # board stale
    assert all_good(s, good_board(s, L1), c) is None  # a locked leg absent
    planned = make_leg("UT", "RTD", "08:50", "09:24", number="5678", provenance=Provenance.PLANNED)
    assert all_good(s, good_board(s, L1, planned), c) is None
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_arr=5)  # margin 5 < 4 + 2
    assert all_good(s, good_board(s, late, L2), c) is None
    slow = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=4, delay_arr=4)
    assert all_good(s, good_board(s, slow, L2), c) is None  # delayed is not ok


def test_no_all_good_later_when_data_turns_good():
    s = occ(LOCK, all_good_evaluated=True)  # the first lock found it not proven
    assert all_good(s, good_board(s, L1, L2), ctx(at("08:02"))) is None


def test_all_good_judges_a_leg_on_the_newest_answer_that_shows_it():
    from builders import make_connection

    s = occ(LOCK)
    late = make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=4, delay_arr=4)
    q = Query("AAA", "RTD", at("08:00"))
    old = ProviderAnswer(q, (make_connection([L1, L2]),), at("08:00"))
    new = ProviderAnswer(q, (make_connection([late, L2]),), at("08:01"))
    for answers in ((old, new), (new, old)):
        assert all_good(s, replace(board(s), answers=answers), ctx(at("08:02"))) is None


def test_all_good_withholds_on_stale_answer_even_with_fresh_poll():
    s = occ(LOCK)
    q = Query("AAA", "RTD", at("08:00"))
    old = ProviderAnswer(q, (make_connection([L1, L2]),), at("08:00"))
    b = replace(board(s), answers=(old,))
    assert all_good(s, b, ctx(at("08:02"))) is not None
    assert all_good(s, b, ctx(at("08:11"))) is None  # poll health may be fresh, the answer is not


def test_all_good_not_rescued_by_older_answer_when_newest_is_uncertain():
    s = occ(LOCK)
    q = Query("AAA", "RTD", at("08:00"))
    amb = replace(L1, train=replace(L1.train, train_number="other"))
    old = ProviderAnswer(q, (make_connection([L1, L2]),), at("08:00"))
    new = ProviderAnswer(q, (make_connection([L1, L2]), make_connection([amb, L2])), at("08:01"))
    assert all_good(s, replace(board(s), answers=(old, new)), ctx(at("08:02"))) is None

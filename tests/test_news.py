from dataclasses import replace
from datetime import timedelta

from builders import make_connection, make_leg
from test_lifecycle_follow import L1, L2, R2, at, board, ctx, journey, occ

from train_commute_companion.classify import classify_leg
from train_commute_companion.lifecycle import OccurrenceState
from train_commute_companion.model import (
    EventKind,
    Provenance,
    Severity,
    TransferCheck,
    TransferState,
)
from train_commute_companion.news import (
    Fact,
    alert_key,
    all_good,
    announced,
    announced_delays,
    escalations,
    key_legs,
    severity,
    unsent,
)
from train_commute_companion.providers.base import ProviderAnswer, Query

LOCK = journey([L1, L2])
OK_T = TransferCheck(TransferState.OK, 10, 4, "ok")


def key(leg1=L1, transfer=OK_T, target=None, last=None) -> str:
    return keyed(leg1, transfer, target, last)[0]


def keyed(leg1=L1, transfer=OK_T, target=None, last=None):
    """The key and the delays it states, starting from what the last message stated."""
    cls = [classify_leg(leg1, "UT"), classify_leg(L2, "RTD")]
    delays = announced_delays(last, key_legs(cls, target))
    return alert_key(LOCK, cls, [transfer], target, delays), delays


def sev(kind, minutes=None, **k):
    d = {"breaks_transfer": False, "past_latest_arrival": False, "is_tomorrow": False} | k
    return severity(kind, minutes_to_departure=minutes, **d)


def test_unmapped_kind_is_loud():
    assert sev(EventKind.CUT_SHORT) is Severity.LOUD
    assert sev(EventKind.ALL_GOOD) is Severity.SILENT


def test_offline_and_earlier_train_withdrawn_are_silent():  # table 7.9
    assert sev(EventKind.OFFLINE) is Severity.SILENT
    assert sev(EventKind.EARLIER_TRAIN_WITHDRAWN) is Severity.SILENT


def test_transfer_at_risk_silent_then_loud_inside_30_min_once():  # rule 4
    assert sev(EventKind.TRANSFER_AT_RISK, 45) is Severity.SILENT
    assert sev(EventKind.TRANSFER_AT_RISK, 30) is Severity.LOUD
    facts = [Fact("risk:k", EventKind.TRANSFER_AT_RISK, departure=at("08:12"))]
    ledger = {"risk:k": Severity.SILENT}
    assert escalations(ledger, facts, at("07:12")) == []  # 60 min out
    assert escalations(ledger, facts, at("07:50")) == ["risk:k"]
    assert escalations({"risk:k": Severity.LOUD}, facts, at("07:50")) == []


def test_escalation_times_each_fact_by_its_own_departure():
    # an at-risk change into the 08:50 onward train is 50 min out at 08:00, not 12
    facts = [Fact("risk:2", EventKind.TRANSFER_AT_RISK, departure=at("08:50"))]
    assert escalations({"risk:2": Severity.SILENT}, facts, at("08:00")) == []
    assert escalations({"risk:2": Severity.SILENT}, facts, at("08:20")) == ["risk:2"]


def test_works_escalate_only_when_the_day_is_tomorrow():
    led = {"w": Severity.SILENT}
    later = Fact("w", EventKind.WORKS_AHEAD, day=at("08:00").date() + timedelta(days=3))
    tomorrow = Fact("w", EventKind.WORKS_AHEAD, day=at("08:00").date() + timedelta(days=1))
    assert escalations(led, [later], at("08:00")) == []
    assert escalations(led, [tomorrow], at("08:00")) == ["w"]


def test_delay_escalates_when_it_breaks_a_transfer_or_passes_latest_arrival():
    led = {"d": Severity.SILENT}
    assert escalations(led, [Fact("d", EventKind.DELAYED)], at("08:00")) == []
    assert escalations(led, [Fact("d", EventKind.DELAYED, breaks_transfer=True)], at("08:00"))
    late = Fact("d", EventKind.DELAYED, past_latest_arrival=True)
    assert escalations(led, [late], at("08:00")) == ["d"]


def test_platform_change_loud_within_15_min():
    assert sev(EventKind.PLATFORM_CHANGE, 16) is Severity.SILENT
    assert sev(EventKind.PLATFORM_CHANGE, 15) is Severity.LOUD


def test_delayed_and_works_ahead_are_silent_unless_they_matter():
    assert sev(EventKind.DELAYED) is Severity.SILENT
    assert sev(EventKind.DELAYED, breaks_transfer=True) is Severity.LOUD
    assert sev(EventKind.DELAYED, past_latest_arrival=True) is Severity.LOUD
    assert sev(EventKind.WORKS_AHEAD) is Severity.SILENT
    assert sev(EventKind.WORKS_AHEAD, is_tomorrow=True) is Severity.LOUD


def late(dep: int) -> object:
    return make_leg("AAA", "UT", "08:12", "08:40", number="1234", delay_dep=dep)


def test_delay_is_news_by_movement_since_last_announced():  # 7.5
    base = key()
    assert key(late(2)) == base  # under one step from on time
    k3, said3 = keyed(late(3))
    assert k3 != base
    assert key(late(5), last=said3) == k3  # +3 to +5: under a step from what was said
    k5, said5 = keyed(late(5))
    assert key(late(6), last=said5) == k5  # +5 to +6 after announcing +5: no news
    assert key(late(2), last=said3) == k3  # +3 to +2: not yet back to normal
    assert key(late(1), last=said3) == base  # back to normal at 1 min or less
    assert key(late(6), last=said3) != k3  # one full step later


def test_announced_delay_rules():
    assert announced(None, 2) is None and announced(None, 3) == 3
    assert announced(5, 6) == 5 and announced(3, 2) == 3 and announced(3, 1) == 0
    assert announced(6, 2) == 2  # nearly on time is news
    assert announced(4, None) == 4  # no reported time states nothing new


def test_key_changes_with_transfer_state_and_replacement():
    assert key(transfer=TransferCheck(TransferState.AT_RISK, 5, 4, "x")) != key()
    assert key(target=make_connection([R2])) != key()


def test_replacement_keyed_by_planned_departure_onward_trains_and_platform():  # 7.5
    same_number = make_leg("UT", "RTD", "09:35", "10:09", number="6666")
    assert key(target=make_connection([R2])) != key(target=make_connection([same_number]))
    onward = make_connection([make_leg("UT", "BBB", "09:05", "09:20", number="6666"), L2])
    assert key(target=make_connection([R2])) != key(target=onward)
    moved = replace(R2, stops=(replace(R2.stops[0], actual_track="7"), *R2.stops[1:]))
    assert key(target=make_connection([R2])) != key(target=make_connection([moved]))
    k, said = keyed(target=make_connection([R2]))
    r2_late = make_leg("UT", "RTD", "09:05", "09:39", number="6666", delay_dep=3)
    assert key(target=make_connection([r2_late]), last=said) != k  # replacement departs later
    r2_wobble = make_leg("UT", "RTD", "09:05", "09:39", number="6666", delay_arr=1)
    assert key(target=make_connection([r2_wobble]), last=said) == k


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


def test_changed_cut_short_endpoint_is_news_again():  # 7.5: the key states the endpoint
    from test_lifecycle_follow import DIRECT, FROM_BBB, FROM_UT, T_BBB, T_UT, ans, conn, poll
    from test_lifecycle_follow import direct_riding as riding

    c = ctx(at("08:14"), DIRECT)
    t1 = poll(riding(), c, ans("AAA", conn(T_BBB)), ans("BBB", conn(FROM_BBB)))
    answers = (ans("AAA", conn(T_UT)), ans("UT", conn(FROM_UT)), ans("BBB", conn(FROM_BBB)))
    t2 = poll(t1.state, c, *answers)
    ledger: dict[str, Severity] = {}
    first = unsent(ledger, t1.events)
    ledger.update({e.key: e.severity for e in first})
    second = unsent(ledger, t2.events)
    assert [e.kind for e in second] == [EventKind.CUT_SHORT] and "UT" in second[0].text
    # the same warning on the next poll is a repeat, not news
    ledger.update({e.key: e.severity for e in second})
    assert unsent(ledger, t2.events) == []


def answer_at(when: str, *conns):
    q = Query("AAA", "RTD", at("08:00"))
    return ProviderAnswer(q, tuple(conns), at(when))


def test_no_all_good_when_newest_answer_omits_a_locked_leg():  # rule 20
    s = occ(LOCK)
    old = answer_at("08:00", make_connection([L1, L2]))
    new = answer_at("08:01", make_connection([L1]))
    assert all_good(s, replace(board(s), answers=(old, new)), ctx(at("08:02"))) is None


def test_no_all_good_when_answers_disagree_on_identity():  # 6.1 across answers
    s = occ(LOCK)
    other = replace(L1, train=replace(L1.train, train_number="4321"))
    a = answer_at("08:01", make_connection([L1, L2]))
    b = answer_at("08:00", make_connection([other, L2]))
    for answers in ((a, b), (b, a)):
        assert all_good(s, replace(board(s), answers=answers), ctx(at("08:02"))) is None


def test_no_all_good_when_any_copy_is_cancelled_whatever_the_order():
    s = occ(LOCK)
    cancelled = make_leg("AAA", "UT", "08:12", "08:40", number="1234", cancelled=True)
    one = answer_at("08:01", make_connection([L1, L2]), make_connection([cancelled, L2]))
    two = answer_at("08:01", make_connection([cancelled, L2]), make_connection([L1, L2]))
    for a in (one, two):
        assert all_good(s, replace(board(s), answers=(a,)), ctx(at("08:02"))) is None


def test_no_all_good_after_failed_polls_since_the_last_success():  # 7.11, rule 20
    from train_commute_companion.health import PollRecord

    s = occ(LOCK)
    b = good_board(s, L1, L2)
    ok = PollRecord(at("08:00"), True, None)
    for err in ("provider", "auth", "timeout"):
        failed = (ok, PollRecord(at("08:01"), False, err))  # type: ignore[arg-type]
        assert all_good(s, replace(b, polls=failed), ctx(at("08:02"))) is None
    two = (ok, PollRecord(at("08:01"), False, "provider"), PollRecord(at("08:02"), False, "parse"))
    assert all_good(s, replace(b, polls=two), ctx(at("08:02"))) is None
    assert all_good(s, replace(b, polls=(ok,)), ctx(at("08:02"))) is not None

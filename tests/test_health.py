from datetime import date, datetime, timedelta

from builders import AMS

from train_commute_companion.health import (
    PollRecord,
    WatchedLeg,
    board_health,
    cannot_see,
    offline,
)
from train_commute_companion.model import Observation, Provenance, TrainId

NOW = datetime(2026, 10, 27, 8, 0, tzinfo=AMS)
REPORTED, PLANNED, MISSING = Provenance.REPORTED, Provenance.PLANNED, Provenance.MISSING


def mins(n: int) -> timedelta:
    return timedelta(minutes=n)


def leg(
    *,
    in_minutes: int = 20,
    dep_prov: Provenance = REPORTED,
    arr_prov: Provenance = REPORTED,
    obs: Observation = Observation.SEEN,
    riding: bool = False,
    seen_ago: int | None = 1,
) -> WatchedLeg:
    dep = NOW + mins(in_minutes)
    return WatchedLeg(
        TrainId(date(2026, 10, 27), "1234", dep),
        dep,
        riding,
        obs,
        dep_prov,
        arr_prov,
        None if seen_ago is None else NOW - mins(seen_ago),
    )


def health(records: list[PollRecord]):
    return board_health(records, NOW)


def ok(ago: int) -> PollRecord:
    return PollRecord(NOW - mins(ago), True, None)


def bad(ago: int, error="provider") -> PollRecord:
    return PollRecord(NOW - mins(ago), False, error)


def fresh():
    return health([ok(1)])


def test_actual_equal_to_planned_is_not_proof_but_is_fine_when_fresh():
    assert cannot_see([leg(dep_prov=REPORTED, in_minutes=20)], fresh(), NOW)[0] is False


def test_planned_only_within_30_min_is_loud():  # OQ2 = A
    assert cannot_see([leg(dep_prov=PLANNED, in_minutes=20)], fresh(), NOW)[0]


def test_missing_departure_within_30_min_is_loud():
    assert cannot_see([leg(dep_prov=MISSING, in_minutes=20)], fresh(), NOW)[0]


def test_planned_only_45_min_out_is_not_yet():
    assert not cannot_see([leg(dep_prov=PLANNED, in_minutes=45)], fresh(), NOW)[0]


def test_planned_arrival_matters_only_while_riding():
    assert not cannot_see([leg(arr_prov=PLANNED)], fresh(), NOW)[0]
    assert cannot_see([leg(arr_prov=PLANNED, riding=True)], fresh(), NOW)[0]


def test_departed_leg_needs_no_departure_time():
    departed = leg(in_minutes=-3, dep_prov=PLANNED, riding=True)
    assert not cannot_see([departed], fresh(), NOW)[0]


def test_riding_leg_far_from_departure_counts():
    h = health([ok(9)])
    assert cannot_see([leg(in_minutes=120, riding=True)], h, NOW)[0]


def test_two_failed_polls():
    h = health([ok(6), bad(4), bad(2)])
    assert h.consecutive_failures == 2
    assert cannot_see([leg()], h, NOW)[0]


def test_one_failed_poll_with_fresh_board_is_fine():
    h = health([ok(3), bad(1)])
    assert h.fresh and h.consecutive_failures == 1
    assert not cannot_see([leg()], h, NOW)[0]


def test_board_older_than_5_min():
    assert cannot_see([leg()], health([ok(5)]), NOW)[0]
    assert health([ok(4)]).fresh  # 4 min is still fresh


def test_no_polls_at_all_is_not_fresh():
    assert cannot_see([leg()], health([]), NOW)[0]


def test_uncertain_or_missing_leg():
    for o in (Observation.UNCERTAIN, Observation.MISSING):
        assert cannot_see([leg(obs=o)], fresh(), NOW)[0]


def test_retained_leg_absent_while_new_lock_fresh_is_unseen():
    assert cannot_see([leg(seen_ago=9, riding=True)], fresh(), NOW)[0]
    assert cannot_see([leg(seen_ago=None, riding=True)], fresh(), NOW)[0]


def test_no_legs_in_zone_is_quiet_even_when_board_is_stale():
    assert cannot_see([leg(in_minutes=90)], health([ok(30)]), NOW) == (False, "")


def test_auth_failure_flag():
    assert health([ok(3), bad(1, "auth")]).auth_failed
    assert not health([bad(3, "auth"), ok(1)]).auth_failed


def test_offline_only_outside_zone_and_not_riding():
    stale = health([ok(30)])
    assert offline(stale, False)
    assert not offline(stale, True)
    assert not offline(fresh(), False)


def test_departed_leg_not_ridden_is_finished_and_ignored():
    gone = leg(in_minutes=-3, riding=False)
    assert not cannot_see([gone], health([ok(20)]), NOW)[0]


def test_rate_errors_count_as_failed_polls():  # 7.1: backoff on 429 counts toward board health
    rate = PollRecord(at=NOW - timedelta(minutes=2), ok=False, error="rate")
    h = health([ok(6), PollRecord(at=NOW - timedelta(minutes=4), ok=False, error="rate"), rate])
    assert h.consecutive_failures == 2

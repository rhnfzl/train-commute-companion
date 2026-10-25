from datetime import UTC, date, time

from train_commute_companion.calendar import is_commute_day, nl_public_holidays, resolve_local


def test_nl_holidays_2026():
    assert nl_public_holidays(2026) == {
        date(2026, 1, 1),
        date(2026, 4, 6),
        date(2026, 4, 27),
        date(2026, 5, 14),
        date(2026, 5, 25),
        date(2026, 12, 25),
        date(2026, 12, 26),
    }


def test_kings_day_moves_when_sunday():
    assert date(2025, 4, 26) in nl_public_holidays(2025)
    assert date(2025, 4, 27) not in nl_public_holidays(2025)


def test_extra_date_counts_and_holiday_skipped():
    assert is_commute_day(
        date(2026, 10, 31), frozenset({2, 3}), frozenset({date(2026, 10, 31)}), True
    )
    assert not is_commute_day(date(2026, 12, 25), frozenset({5}), frozenset(), True)
    assert is_commute_day(date(2026, 12, 25), frozenset({5}), frozenset(), False)


def test_autumn_repeated_hour():
    s = resolve_local(date(2026, 10, 25), time(2, 30), edge="start")
    e = resolve_local(date(2026, 10, 25), time(2, 30), edge="end")
    assert (e.astimezone(UTC) - s.astimezone(UTC)).total_seconds() == 3600


def test_spring_gap_moves_forward():
    t = resolve_local(date(2027, 3, 28), time(2, 30), edge="start")
    assert t.hour == 3 and t.minute == 0


def test_normal_time_is_unchanged_for_both_edges():
    for edge in ("start", "end"):
        t = resolve_local(date(2026, 6, 1), time(8, 12), edge=edge)
        assert (t.hour, t.minute, t.utcoffset().total_seconds()) == (8, 12, 7200)


def test_easter_other_years():
    assert date(2027, 3, 29) in nl_public_holidays(2027)
    assert date(2024, 4, 1) in nl_public_holidays(2024)

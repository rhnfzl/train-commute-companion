from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from train_commute_companion.calendar import TZ
from train_commute_companion.model import Config, Direction
from train_commute_companion.profile import parse_profiles
from train_commute_companion.schedule import (
    dated_schedule,
    effective_first_train,
    schedules_active,
)

BASE = """
profiles:
  - id: "p1"
    name: "office_a"
    days: [tue, sun, sat]
    home_station: "AAA"
    work_station: "UT"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def profile(**kw):
    ps, errs = parse_profiles(BASE, None)
    assert errs == []
    p = ps[0]
    for key, (first, window, offset) in kw.items():
        j = p.outbound if key == "out" else p.return_
        h, m = map(int, first.split(":"))
        w = tuple(time(*map(int, x.split(":"))) for x in window) if window else j.window
        j = replace(j, first_train=time(h, m), window=w, day_offset=offset)
        p = replace(p, **{"outbound" if key == "out" else "return_": j})
    return p


def test_window_anchored_on_previous_evening():
    p = profile(out=("00:30", ("22:00", "01:30"), 0))
    s = dated_schedule(p, Direction.OUTBOUND, date(2026, 10, 28), None)
    assert s.window_start == dt("2026-10-27 22:00") and s.window_end == dt("2026-10-28 01:30")


def test_return_day_offset_1_service_date():
    p = profile(ret=("04:00", None, 1))
    s = dated_schedule(p, Direction.RETURN, date(2026, 10, 27), None)
    assert s.key.service_date == date(2026, 10, 28)
    assert s.first_departure == dt("2026-10-28 04:00")


def test_auto_lock_time():
    s = dated_schedule(profile(), Direction.OUTBOUND, date(2026, 10, 27), None)
    assert s.auto_lock_at == dt("2026-10-27 07:47")  # 08:12 - 10 - 15


def test_adoption_moves_window():
    s = dated_schedule(profile(), Direction.OUTBOUND, date(2026, 10, 27), time(8, 15))
    assert s.window_start == dt("2026-10-27 07:15") and s.first_departure == dt("2026-10-27 08:15")
    assert s.window_end == dt("2026-10-27 10:15")
    assert effective_first_train(profile().outbound, None) == time(8, 12)


def utc(d: datetime) -> datetime:
    return d.astimezone(UTC)


def test_dst_autumn_night_window_grows():
    p = profile(out=("00:05", ("22:00", "02:30"), 0))
    s = dated_schedule(p, Direction.OUTBOUND, date(2026, 10, 25), None)
    assert utc(s.window_start) == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
    assert utc(s.window_end) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # second 02:30


def test_dst_spring_night_gap_moves_to_first_instant_after():
    p = profile(out=("02:30", ("00:00", "04:00"), 0))
    s = dated_schedule(p, Direction.OUTBOUND, date(2026, 3, 29), None)
    assert utc(s.first_departure) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 03:00 local
    assert utc(s.window_end) - utc(s.window_start) == timedelta(hours=3)


def test_active_window_looks_back_across_midnight():
    cfg = Config(1, (profile(out=("23:40", ("22:00", "01:30"), 0)),))
    got = schedules_active(cfg, {}, dt("2026-10-28 00:30"))
    assert [(s.key.direction, s.key.service_date) for s in got] == [
        (Direction.OUTBOUND, date(2026, 10, 27))
    ]
    assert schedules_active(cfg, {}, dt("2026-10-28 04:00")) == []


def test_active_window_opens_evening_before_commute_day():
    cfg = Config(1, (profile(out=("00:30", ("22:00", "01:30"), 0)),))
    got = schedules_active(cfg, {}, dt("2026-10-26 22:30"))
    assert [s.key.service_date for s in got] == [date(2026, 10, 27)]


def test_active_uses_baseline():
    cfg = Config(1, (profile(),))
    adopted = {("p1", Direction.OUTBOUND): time(8, 15)}
    assert len(schedules_active(cfg, {}, dt("2026-10-27 07:13"))) == 1
    assert schedules_active(cfg, adopted, dt("2026-10-27 07:13")) == []
    assert len(schedules_active(cfg, adopted, dt("2026-10-27 07:15"))) == 1


def test_dst_lock_time_uses_first_instant_autumn_and_gap_spring():
    p = profile(out=("02:55", ("00:00", "04:00"), 0))
    a = dated_schedule(p, Direction.OUTBOUND, date(2026, 10, 25), None)
    assert utc(a.auto_lock_at) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # first 02:30
    s = dated_schedule(p, Direction.OUTBOUND, date(2026, 3, 29), None)
    assert utc(s.auto_lock_at) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 02:30 missing -> 03:00


def test_active_compares_instants_in_the_repeated_autumn_hour():
    cfg = Config(1, (profile(out=("00:05", ("22:00", "02:30"), 0)),))
    first_0245 = datetime(2026, 10, 25, 2, 45, tzinfo=TZ, fold=0)  # before the second 02:30
    second_0245 = datetime(2026, 10, 25, 2, 45, tzinfo=TZ, fold=1)
    assert [s.key.direction for s in schedules_active(cfg, {}, first_0245)] == [Direction.OUTBOUND]
    assert schedules_active(cfg, {}, second_0245) == []


def test_window_is_half_open():
    cfg = Config(1, (profile(),))
    assert len(schedules_active(cfg, {}, dt("2026-10-27 07:12"))) == 1
    assert schedules_active(cfg, {}, dt("2026-10-27 10:12")) == []

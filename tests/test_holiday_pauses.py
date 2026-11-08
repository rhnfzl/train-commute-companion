from datetime import date, timedelta

import pytest
from builders import make_connection
from lifecycle_fixtures import L1, L2X
from runtime_rig import DAY, PROFILE, started

from train_commute_companion.calendar import TZ
from train_commute_companion.lookahead_probe import probe_dates
from train_commute_companion.model import Config, Direction, EventKind, Profile
from train_commute_companion.overlap import find_overlaps
from train_commute_companion.profile import parse_profiles
from train_commute_companion.schedule import schedules_active
from train_commute_companion.serde import decode, encode

BASE = """
profiles:
  - id: p1
    name: office
    days: [mon, tue, wed, thu, fri, sat, sun]
    skip_public_holidays: false
    home_station: UT
    work_station: RTD
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "17:52", buffer_min: 5}
"""


def paused(text='[{start: "2026-10-27", end: "2026-10-28"}]', base=BASE):
    return parse_profiles(base + f"    pauses: {text}\n", None)


def test_pause_ranges_load_as_dates_and_round_trip():
    profiles, errors = paused()
    assert errors == []
    p = profiles[0]
    assert p.pauses == ((date(2026, 10, 27), date(2026, 10, 28)),)
    assert decode(Profile, encode(p)) == p


def test_profile_without_pauses_and_old_snapshot_keep_working():
    profiles, errors = parse_profiles(BASE, None)
    assert errors == []
    p = profiles[0]
    assert p.pauses == ()
    old = encode(p)
    old.pop("pauses")
    assert decode(Profile, old) == p


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ("null", "pauses"),
        ("{}", "pauses"),
        ("[false]", "pauses[0]"),
        ('[{start: "2026-10-28", end: "2026-10-27"}]', "pauses[0].end"),
        ('[{start: "2026-02-30", end: "2026-10-27"}]', "pauses[0].start"),
        ('[{start: "2026-10-27"}]', "pauses[0].end"),
        ('[{start: 20261027, end: "2026-10-28"}]', "pauses[0].start"),
        ('[{start: "0001-01-01", end: "2026-10-28"}]', "pauses[0].start"),
        ('[{start: "2026-10-27", end: "2026-10-28", until: "2026-10-29"}]', "pauses[0].until"),
    ],
)
def test_invalid_pause_range_is_refused(value, path):
    profiles, errors = paused(value)
    assert profiles == []
    assert f"profiles[0].{path}" in {e.path for e in errors}


def test_pauses_are_inclusive_override_extra_dates_and_resume_automatically():
    from datetime import datetime

    base = BASE + '    extra_dates: ["2026-10-27"]\n'
    profiles, errors = paused(base=base)
    assert errors == []
    cfg = Config(1, tuple(profiles))
    for day, expected in ((26, 1), (27, 0), (28, 0), (29, 1)):
        now = datetime(2026, 10, day, 8, 0, tzinfo=TZ)
        assert len(schedules_active(cfg, {}, now)) == expected


def test_pause_applies_to_the_return_service_date_after_midnight():
    from datetime import datetime

    base = BASE.replace(
        'first_train: "17:52", buffer_min: 5', 'first_train: "04:00", buffer_min: 5, day_offset: 1'
    )
    profiles, errors = paused('[{start: "2026-10-28", end: "2026-10-28"}]', base)
    assert errors == []
    cfg = Config(1, tuple(profiles))
    assert schedules_active(cfg, {}, datetime(2026, 10, 28, 4, 0, tzinfo=TZ)) == []
    got = schedules_active(cfg, {}, datetime(2026, 10, 29, 4, 0, tzinfo=TZ))
    assert [s.key.direction for s in got] == [Direction.RETURN]


def test_separate_pause_ranges_leave_the_gap_available():
    from datetime import datetime

    profiles, errors = paused(
        '[{start: "2026-10-27", end: "2026-10-27"}, {start: "2026-10-29", end: "2026-10-30"}]'
    )
    assert errors == []
    cfg = Config(1, tuple(profiles))
    assert len(schedules_active(cfg, {}, datetime(2026, 10, 28, 8, 0, tzinfo=TZ))) == 1
    assert schedules_active(cfg, {}, datetime(2026, 10, 29, 8, 0, tzinfo=TZ)) == []


def test_lookahead_skips_paused_dates_and_finds_next_three_commutes():
    profiles, errors = paused()
    assert errors == []
    assert probe_dates(profiles[0], date(2026, 10, 26)) == [
        date(2026, 10, 29),
        date(2026, 10, 30),
        date(2026, 10, 31),
    ]


def test_lookahead_filters_each_direction_by_its_service_date():
    base = BASE.replace(
        'first_train: "17:52", buffer_min: 5', 'first_train: "04:00", buffer_min: 5, day_offset: 1'
    )
    profiles, errors = paused('[{start: "2026-10-28", end: "2026-10-28"}]', base)
    assert errors == []
    p = profiles[0]
    assert probe_dates(p, date(2026, 10, 26), direction=Direction.RETURN) == [
        date(2026, 10, 28),
        date(2026, 10, 29),
        date(2026, 10, 30),
    ]
    assert probe_dates(p, date(2026, 10, 26), direction=Direction.OUTBOUND) == [
        date(2026, 10, 27),
        date(2026, 10, 29),
        date(2026, 10, 30),
    ]


@pytest.mark.parametrize("paused_id", ["p1", "p2"])
def test_a_paused_one_off_date_does_not_refuse_other_profiles(paused_id):
    from dataclasses import replace

    profiles, errors = parse_profiles(BASE, None)
    assert errors == []
    a = replace(profiles[0], days=frozenset({2}))
    b = replace(a, id="p2", name="other", days=frozenset({4}), extra_dates=frozenset({DAY}))
    span = ((DAY, DAY),)
    pair = [replace(p, pauses=span) if p.id == paused_id else p for p in (a, b)]
    assert find_overlaps(pair, date(2026, 10, 26)) == []


def pause_runtime_profile():
    return PROFILE.replace(
        '    name: "NAME"',
        '    name: "office_a"\n    pauses: [{start: "2026-10-27", end: "2026-10-28"}]',
    )


async def test_adding_pause_drops_an_unlocked_occurrence(tmp_path):
    rig = await started(tmp_path, "07:15")
    assert rig.occ().lock is None
    rig.write(text=pause_runtime_profile())
    await rig.rt.reload()
    await rig.rt.poll_once()
    assert rig.occ() is None


async def test_live_lookahead_never_requests_a_paused_service_date(tmp_path):
    rig = await started(tmp_path)
    text = pause_runtime_profile().replace('start: "2026-10-27"', 'start: "2026-10-28"')
    rig.write(text=text)
    await rig.rt.reload()
    rig.provider.calls.clear()
    await rig.rt.lookahead_once()
    assert rig.provider.calls
    assert date(2026, 10, 28) not in {q.at.date() for q in rig.provider.calls}


async def test_adding_pause_keeps_a_locked_journey_safety_monitoring(tmp_path):
    rig = await started(tmp_path)
    lock = rig.occ().lock
    assert lock is not None
    rig.write(text=pause_runtime_profile())
    await rig.rt.reload()
    assert rig.occ().lock == lock
    rig.provider.world = [make_connection([L1, L2X])]
    rig.clock.set("07:52")
    await rig.rt.poll_once()
    assert EventKind.CANCELLED in {e.kind for e in rig.rt.events_log}
    restored = rig.store.load()
    assert restored.config.profiles[0].pauses == ((DAY, DAY + timedelta(days=1)),)

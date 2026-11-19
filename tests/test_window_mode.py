"""Window mode, its pick setting and the minimum stay, run on the runtime rig (0.3.0)."""

from datetime import timedelta

from builders import make_connection, make_leg
from runtime_rig import DAY, Rig, at

from train_commute_companion.location import NO_EVIDENCE, Evidence, LocationEvent
from train_commute_companion.model import DirectionState, EventKind, Severity
from train_commute_companion.state import LockSource

OUT_KEY = "p1|outbound|2026-10-27"
RET_KEY = "p1|return|2026-10-27"
PROFILE = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon, tue, wed, thu, fri]
    home_station: "AAA"
    work_station: "RTD"
    outbound: OUT
    return: RET
    location: LOC
"""
LOCATED = "{source: ha, entity: person.me}"
WINDOW_OUT = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10}'
FIXED_RET = '{first_train: "17:52", buffer_min: 5}'


def mornings() -> list:
    """AAA to RTD at 06:45 and every half hour from 07:00, 38 min each."""
    deps = ["06:45", *(f"{h:02d}:{m:02d}" for h in range(7, 11) for m in (0, 30))]
    out = []
    for i, dep in enumerate(deps):
        h, m = int(dep[:2]), int(dep[3:]) + 38
        arr = f"{h + m // 60:02d}:{m % 60:02d}"
        out.append(make_connection([make_leg("AAA", "RTD", dep, arr, number=f"5{i:02d}")]))
    return out


def evenings() -> list:
    """RTD to AAA every half hour from 14:00 to 21:00, 40 min each."""
    out = []
    for i, h in enumerate(range(14, 21)):
        for m in (0, 30):
            dep = f"{h:02d}:{m:02d}"
            arr = f"{h + (m + 40) // 60:02d}:{(m + 40) % 60:02d}"
            out.append(make_connection([make_leg("RTD", "AAA", dep, arr, number=f"8{i}{m}")]))
    return out


async def rig_for(
    tmp_path,
    out: str = WINDOW_OUT,
    ret: str = FIXED_RET,
    loc: str = LOCATED,
    extra: str = "",
    now: str = "06:30",
    world: list | None = None,
) -> Rig:
    rig = Rig(tmp_path, now)
    text = PROFILE.replace("OUT", out).replace("RET", ret).replace("LOC", loc)
    rig.write(text=text + extra)
    rig.provider.world = mornings() + evenings() if world is None else world
    await rig.rt.start()
    return rig


async def report(
    rig: Rig, kind: str, hhmm: str, zone: str = "home", ev: Evidence = NO_EVIDENCE
) -> None:
    rig.clock.set(hhmm)
    await rig.rt.report_location("p1", LocationEvent(zone, kind, at(hhmm), ev))  # type: ignore[arg-type]


def first_train(rig: Rig, key: str = OUT_KEY) -> str | None:
    s = rig.occ(key)
    return (
        None if s is None or s.lock is None else f"{s.lock.legs[0].train.planned_departure:%H:%M}"
    )


def told(rig: Rig, kind: EventKind) -> list[tuple[str, Severity]]:
    return [(e.body, e.severity) for e in rig.rt.events_log if e.kind is kind]


def loud(rig: Rig) -> list[EventKind]:
    return [e.kind for e in rig.rt.events_log if e.severity is Severity.LOUD]


async def test_window_earliest_locks_first_train_from_window_start_loud_miss(tmp_path):
    rig = await rig_for(tmp_path)
    await rig.run_until("06:37")  # the window opens 25 min (buffer + 15) before 07:00
    assert first_train(rig) == "07:00"  # never the 06:45 before the window
    (shown,) = [o for o in rig.rt.view().occurrences if o.key.service_date == DAY]
    assert shown.usual_departure is None
    assert [f"{t:%H:%M}" for t in shown.window or ()] == ["07:00", "10:00"]
    await report(rig, "report", "06:55")
    await rig.run_until("07:12")
    await report(rig, "still", "07:12")  # still home 12 min after the 07:00 left
    (miss,) = told(rig, EventKind.MISSED_FIRST_TRAIN)
    assert miss[1] is Severity.LOUD and miss[0].startswith("you missed the 07:00")
    assert "Leave by" in miss[0]
    assert first_train(rig) == "07:30"


async def test_window_earliest_silent_misses_are_silent(tmp_path):
    out = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, pick: earliest_silent}'
    rig = await rig_for(tmp_path, out=out)
    await rig.run_until("06:55")
    await report(rig, "report", "06:55")
    await rig.run_until("07:12")
    await report(rig, "still", "07:12")
    assert [s for _, s in told(rig, EventKind.MISSED_FIRST_TRAIN)] == [Severity.SILENT]
    assert first_train(rig) == "07:30"


async def test_quiet_repeat_misses_still_applies_in_window_mode(tmp_path):
    rig = await rig_for(tmp_path, extra="    alerts: {quiet_repeat_misses: true}\n")
    await rig.run_until("06:55")
    await report(rig, "report", "06:55")
    await rig.run_until("07:12")
    await report(rig, "still", "07:12")
    await rig.run_until("07:42")
    await report(rig, "still", "07:42")  # the 07:30 left 12 min ago
    assert [s for _, s in told(rig, EventKind.MISSED_FIRST_TRAIN)] == [
        Severity.LOUD,
        Severity.SILENT,
    ]


async def test_window_no_train_in_window_says_so_once(tmp_path):
    outside = [c for c in mornings() if f"{c.legs[0].train.planned_departure:%H:%M}" == "06:45"]
    rig = await rig_for(tmp_path, world=outside)
    await rig.run_until("09:00")
    assert told(rig, EventKind.NO_CONNECTION) == [
        ("no train found from 07:00 to 10:00, check the NS app.", Severity.LOUD)
    ]
    assert rig.occ(OUT_KEY).lock is None


async def test_window_mode_sends_no_usual_missing_or_left_note(tmp_path):
    rig = await rig_for(tmp_path, loc="null", now="07:40")  # started late: 07:00 already left
    rig.provider.window = timedelta(hours=1)  # like NS: trains that left are not in the answer
    await rig.run_until("09:00")
    assert first_train(rig) == "08:00"  # the earliest one still catchable at 07:40 + 10
    assert told(rig, EventKind.MISSING) == []
    assert not any("your usual" in e.text for e in rig.rt.events_log)


ON_LEAVE = '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, pick: on_leave}'


async def test_pick_on_leave_follows_nothing_until_leaving(tmp_path):
    rig = await rig_for(tmp_path, out=ON_LEAVE)
    rig.provider.window = timedelta(hours=1)  # like NS: only trains from the time asked
    await rig.run_until("08:10")
    assert rig.occ(OUT_KEY).lock is None
    await report(rig, "leave", "08:10")
    s = rig.occ(OUT_KEY)
    assert first_train(rig) == "08:30"  # the next one catchable from 08:10 + 10
    assert s.lock is not None and s.lock.source is LockSource.LOCATION


async def test_pick_on_leave_tap_locks_next_train(tmp_path):
    rig = await rig_for(tmp_path, out=ON_LEAVE)
    rig.provider.window = timedelta(hours=1)
    await rig.run_until("08:10")
    assert await rig.rt.leaving("p1") is None
    await rig.run_until("08:14")
    assert first_train(rig) == "08:30"


async def test_pick_on_leave_window_closes_quietly(tmp_path):
    rig = await rig_for(tmp_path, out=ON_LEAVE)
    await rig.run_until("10:05")
    assert loud(rig) == []
    s = rig.occ(OUT_KEY)
    assert s is None or s.direction_state is DirectionState.DONE


async def test_pick_on_leave_without_location_locks_the_earliest_silently(tmp_path):
    rig = await rig_for(tmp_path, out=ON_LEAVE, loc="null")
    await rig.run_until("06:37")
    assert first_train(rig) == "07:00"


STAY_RET = '{mode: window, window: ["15:00", "20:00"], buffer_min: 5, min_stay_min: 480}'
FIXED_OUT = '{first_train: "08:12", buffer_min: 10}'


def commute_day() -> list:
    out = make_connection([make_leg("AAA", "RTD", "08:12", "08:50", number="812")])
    return [out, *evenings()]


def stay_start(rig: Rig) -> str:
    st = rig.rt.state
    s = st.occurrences[RET_KEY]
    return f"{rig.rt._ctx(s, rig.clock.now()).schedule.first_departure:%H:%M}"


async def ride_to_work(rig: Rig, phone: bool) -> None:
    await rig.run_until("07:58")
    await report(rig, "leave", "07:58")
    await rig.run_until("08:05")
    await report(rig, "enter", "08:05", zone="AAA")
    await rig.run_until("09:00")
    if phone:
        await report(rig, "enter", "09:05", zone="work")
        await rig.run_until("12:00")
        await report(rig, "report", "12:00", zone="work")  # a later report never moves it


async def test_min_stay_starts_at_later_of_window_and_arrival(tmp_path):
    rig = await rig_for(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=True)
    record = rig.rt.state.journey_records[OUT_KEY]
    assert record.seen_at == at("09:05") and record.train_arrival is not None
    await rig.run_until("14:50")  # the return occurrence opens at its unshifted window
    assert stay_start(rig) == "17:05"  # the phone at work, not the 08:50 train arrival
    await rig.run_until("16:44")  # the stay ends at 17:05, watched from 16:45 (lead 20)
    assert not [q for q in rig.provider.calls if q.origin == "RTD"]  # no request in the stay
    assert OUT_KEY not in rig.rt.state.occurrences  # archived; its record still holds
    assert rig.rt.state.journey_records[OUT_KEY].seen_at == at("09:05")
    await rig.run_until("16:50")
    assert first_train(rig, RET_KEY) == "17:30"  # the earliest at or after 17:05


async def test_min_stay_uses_train_arrival_without_phone(tmp_path):
    rig = await rig_for(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=False)
    await rig.run_until("14:50")
    assert stay_start(rig) == "16:50"


async def test_leaving_before_stay_end_still_follows(tmp_path):
    rig = await rig_for(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=True)
    rig.provider.window = timedelta(hours=1)
    await rig.run_until("15:20")
    await report(rig, "report", "15:20", zone="work")
    await report(rig, "leave", "15:30", zone="work")
    s = rig.occ(RET_KEY)
    assert s.lock is not None and s.lock.source is LockSource.LOCATION
    assert first_train(rig, RET_KEY) == "16:00"  # from 15:30 + 5, the stay does not hold it


async def test_tap_before_stay_end_still_follows(tmp_path):
    rig = await rig_for(tmp_path, FIXED_OUT, STAY_RET, now="07:50", world=commute_day())
    await ride_to_work(rig, phone=True)
    rig.provider.window = timedelta(hours=1)
    await rig.run_until("15:20")
    assert await rig.rt.leaving("p1", direction=None) is None
    await rig.run_until("15:24")
    assert first_train(rig, RET_KEY) == "15:30"


async def test_lookahead_skips_window_days(tmp_path):
    out = (
        '{first_train: "08:12", buffer_min: 10, '
        'by_weekday: {thu: {mode: window, window: ["07:00", "10:00"]}}}'
    )
    rig = await rig_for(tmp_path, out, loc="null", now="05:40", world=[])
    await rig.rt.lookahead_once()
    days = {q.at.date() for q in rig.provider.calls if q.origin == "AAA"}
    wed, thu = DAY + timedelta(days=1), DAY + timedelta(days=2)
    assert wed in days and thu not in days


async def test_min_stay_capped_at_latest_time_still_follows_a_train(tmp_path):
    out = '{first_train: "12:22", buffer_min: 10}'
    noon = make_connection([make_leg("AAA", "RTD", "12:22", "13:00", number="1222")])
    late = [c for c in evenings() if f"{c.legs[0].train.planned_departure:%H:%M}" != "20:00"]
    rig = await rig_for(tmp_path, out, STAY_RET, now="11:50", world=[noon, *late])
    await rig.run_until("12:05")
    await report(rig, "enter", "12:05", zone="AAA")
    await rig.run_until("14:50")
    assert stay_start(rig) == "20:00"  # 13:00 + 8 h is past the latest time: capped there
    await rig.run_until("19:42")
    assert first_train(rig, RET_KEY) == "20:30"  # the first one from the latest time on
    assert told(rig, EventKind.NO_CONNECTION) == []


def changes() -> list:
    """AAA to UT to RTD every half hour from 07:00: 20 min, a 10 min change, 20 min."""

    def hm(minute: int) -> str:
        return f"{minute // 60:02d}:{minute % 60:02d}"

    out = []
    for i, start in enumerate(range(7 * 60, 10 * 60, 30)):
        first = make_leg("AAA", "UT", hm(start), hm(start + 20), number=f"1{i:02d}")
        second = make_leg("UT", "RTD", hm(start + 30), hm(start + 50), number=f"2{i:02d}")
        out.append(make_connection([first, second]))
    return out


SILENT_CHANGES = (
    '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, transfers: [UT], '
    "pick: earliest_silent}"
)


async def test_window_silent_miss_at_the_station_and_a_missed_change_stays_loud(tmp_path):
    extra = "    transfer_min: {UT: 4}\n"
    rig = await rig_for(tmp_path, out=SILENT_CHANGES, extra=extra, world=changes())
    await rig.run_until("06:55")
    await report(rig, "enter", "06:55", zone="AAA")
    await rig.run_until("07:12")
    await report(rig, "still", "07:12", zone="AAA")  # the 07:00 left 12 min ago
    assert [s for _, s in told(rig, EventKind.MISSED_FIRST_TRAIN)] == [Severity.SILENT]
    assert first_train(rig) == "07:30"
    await rig.run_until("07:52")
    await report(rig, "enter", "07:52", zone="UT")
    await rig.run_until("08:12")
    await report(rig, "still", "08:12", zone="UT")  # the 08:00 change left 12 min ago
    assert [s for _, s in told(rig, EventKind.MISSED_TRANSFER)] == [Severity.LOUD]

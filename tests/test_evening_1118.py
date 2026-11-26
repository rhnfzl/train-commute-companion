"""The real 2026-11-18 evening, fix by fix, with the real stations: home 's-Hertogenbosch (HT),
work Blerick (BR). Three trains from Blerick were let go, the user drove off at 17:40, sent one
fix about 409 m from Venlo (VL) at 19:50 and rode the 20:06 from Venlo home. That train calls at
Blerick, and at 20:14 the phone reported the Blerick station zone from on board. Up to 17:43 the
fixes go in as `test_never_boarded` resolves them (zone events); from 17:44:04 on they are raw
fixes with the real distances, accuracies and speeds, placed at those distances."""

import math

import pytest
from builders import make_connection, make_leg
from runtime_rig import Rig, at
from test_never_boarded import KEY, loud_after, misses, replay_to_departure

from train_commute_companion.location import NO_EVIDENCE, Evidence, Fix, LocationEvent, Zone
from train_commute_companion.model import DirectionState, EventKind, Severity
from train_commute_companion.view import track_fix

BR = (51.3687, 6.1487)
VL = (51.3634, 6.1718)
HT = (51.6906, 5.2936)


def off(p: tuple[float, float], metres: float, bearing: float) -> tuple[float, float]:
    b = math.radians(bearing)
    dlat = metres * math.cos(b) / 111_320
    dlon = metres * math.sin(b) / (111_320 * math.cos(math.radians(p[0])))
    return p[0] + dlat, p[1] + dlon


WORK = off(BR, 180, 270)  # the office by the station; left at 343 m from the station
HOME = off(HT, 1300, 200)
PLACES = (Zone("work", *WORK, 80.0), Zone("home", *HOME, 100.0))
LOCATED = "{source: ha, entity: person.me}"

PROFILE = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon, tue, wed, thu, fri]
    home_station: "HT"
    work_station: "BR"
    outbound: {first_train: "08:12", buffer_min: 10}
    return: {first_train: "16:36", buffer_min: 2, active_lead_min: 45, window: ["15:45", "21:30"]OTHER}
    location: LOCATION
"""


def world(late: int = 0, late_arr: int | None = None) -> list:
    """Blerick to 's-Hertogenbosch at :06 and :36, as on 11-18 (the 17:06 and 17:36 late), and
    the Venlo trains, which call at Blerick 4 min later. The 20:06 from Venlo has the same number
    at Blerick, so a train taken again from Blerick looks the same by number alone."""
    delays = {"17:06": 3, "17:36": 6}
    out = []
    deps = ("16:36", "17:06", "17:36", "18:06", "18:36", "19:06", "19:36", "20:06", "20:36")
    for i, dep in enumerate(deps):
        arr = f"{int(dep[:2]) + 1}:{dep[3:]}"
        d = delays.get(dep, 0)
        n = f"35{62 + 2 * i}"
        leg = make_leg(
            "BR", "HT", dep, arr, number=n, delay_dep=d, delay_arr=d // 2, stops=("EHV",)
        )
        out.append(make_connection([leg]))
    for dep, at_br, arr, n in (
        ("19:36", "19:40", "20:36", "3679"),
        ("20:06", "20:10", "21:06", "3681"),
        ("20:36", "20:40", "21:36", "3683"),
    ):
        d = late if n == "3681" else 0
        a = (d if late_arr is None else late_arr) if n == "3681" else 0
        late_by = {"number": n, "delay_dep": d, "delay_arr": a}
        venlo = make_leg("VL", "HT", dep, arr, stops=("BR", "EHV"), **late_by)
        blerick = make_leg("BR", "HT", at_br, arr, stops=("EHV",), **late_by)
        out += [make_connection([venlo]), make_connection([blerick])]
    return out


async def rig_for(tmp_path, other: str = "", late: int = 0, late_arr: int | None = None) -> Rig:
    rig = Rig(tmp_path, "15:40")
    text = PROFILE.replace("LOCATION", LOCATED).replace("OTHER", f", {other}" if other else "")
    rig.write(text=text)
    rig.provider.world = world(late, late_arr)
    rig.provider.positions = {"BR": BR, "VL": VL, "HT": HT}
    await rig.rt.start()
    return rig


class Phone:
    """The phone as the HA tracker sends it: raw fixes through the shared `track_fix`."""

    def __init__(self, rig: Rig) -> None:
        self.rig, self.track = rig, None
        self.dropped: list[str] = []

    async def fix(
        self, where: tuple[float, float], when: str, acc: float, speed: float | None = None
    ) -> list[tuple[str, str]]:
        rig = self.rig
        rig.clock.set(when)
        view = rig.rt.view()
        p = next(p for p in view.profiles if p.id == "p1")
        epoch = view.location_epochs.get("p1", 0)
        got = track_fix(view, p, epoch, Fix(*where, at(when), acc), self.track, PLACES)
        events: list[tuple[str, str]] = []
        if got is None:
            self.dropped.append(when)
        else:
            self.track, events = got
            ev = Evidence(speed=speed) if speed is not None else NO_EVIDENCE
            for zone, kind in events:
                await rig.rt.report_location("p1", LocationEvent(zone, kind, at(when), ev))
        beat(rig)
        return list(events)


def beat(rig: Rig) -> None:
    """Back on the rig's 30 s beat, so `run_until` keeps polling every 2 min."""
    t = rig.clock.t
    rig.clock.jump((30 - t.second % 30) % 30 / 60)


def following(rig: Rig) -> tuple[str | None, str, str] | None:
    """(boarding station, where the followed train boards, its number) of the lock."""
    s = rig.occ(KEY)
    if s is None or s.lock is None:
        return None
    x = s.lock.legs[0]
    return s.boarding_station, x.board_station, x.train.train_number


VENLO_2006 = ("VL", "VL", "3681")


async def to_venlo(rig: Rig) -> Phone:
    """15:40 to the one fix near Venlo: the misses and the drive off as `test_never_boarded`
    resolves them, then the real fixes from the last one on the road."""
    await replay_to_departure(rig)
    await rig.rt.poll_once()  # the 17:44 poll, as the rig's beat runs it
    phone = Phone(rig)
    assert await phone.fix(off(BR, 369, 90), "17:44:04", 30, speed=10) == []  # seeds the track
    await rig.run_until("19:50:30")
    assert await phone.fix(off(VL, 409, 0), "19:50:52", 30) == [("VL", "enter")]
    return phone


async def ride_home(rig: Rig, phone: Phone, zone_event: bool, between=None) -> None:
    """From the 20:06 leaving Venlo to home. `zone_event`: the 20:14 Blerick fix also reaches the
    core as a station zone event (HA put the person in zone Work Station), not only as a fix."""
    await rig.run_until("20:14:30")
    await phone.fix(off(BR, 69, 45), "20:14:37", 209)
    if zone_event:
        rig.clock.set("20:14:37")
        await rig.rt.report_location("p1", LocationEvent("BR", "enter", at("20:14:37")))
        beat(rig)
    assert following(rig) == VENLO_2006, "the Blerick fix on board reset the 20:06"
    if between is not None:
        await between()
    path = [(20, 19, 50), (20, 30, 5), (20, 40, 10), (20, 50, 20), (21, 5, 0)]
    for i, (h, m, sec) in enumerate(path):
        f = (i + 1) / (len(path) + 1)
        where = (BR[0] + f * (HT[0] - BR[0]), BR[1] + f * (HT[1] - BR[1]))
        await rig.run_until(f"{h:02d}:{m:02d}:00")
        await phone.fix(where, f"{h:02d}:{m:02d}:{sec:02d}", 1414, speed=30)
        # also past 19:50:52 + 45 min: a known station's train that left stays the lock
        assert following(rig) == VENLO_2006, f"lost the 20:06 at {h:02d}:{m:02d}"
        assert rig.occ(KEY).retained == ()
    await rig.run_until("21:07:00")
    await phone.fix(off(HT, 900, 150), "21:07:20", 267)  # the city zone, too coarse to place
    await rig.run_until("21:09:30")
    await phone.fix(off(HT, 40, 300), "21:09:44", 31)
    await rig.run_until("21:24:30")
    await phone.fix(HOME, "21:24:33", 20)
    await rig.run_until("22:00")


def assert_quiet_end(rig: Rig) -> None:
    assert misses(rig) == [
        ("Missed 16:36", Severity.LOUD),
        ("Missed 17:06", Severity.LOUD),
    ]
    assert loud_after(rig, "17:21:30") == []
    s = rig.occ(KEY)
    assert s is None or s.direction_state is DirectionState.DONE
    record = rig.rt.state.journey_records[KEY]
    assert record.seen_at is not None and at("21:06") <= record.seen_at <= at("21:24:33")


@pytest.mark.parametrize("zone_event", [False, True])
@pytest.mark.parametrize("late", [0, 8])
async def test_1118_known_venlo_follows_the_2006_home(tmp_path, zone_event, late):
    rig = await rig_for(tmp_path, other="other_stations: [VL]", late=late)
    phone = await to_venlo(rig)
    assert following(rig) == VENLO_2006
    assert [e.kind for e in rig.rt.events_log if e.kind is EventKind.STATION_ASK] == []
    await ride_home(rig, phone, zone_event)
    assert_quiet_end(rig)
    record = rig.rt.state.journey_records[KEY]
    assert record.train_arrival == at("21:14" if late else "21:06")
    delays = [e for e in rig.rt.events_log if e.kind is EventKind.DELAYED]
    assert [e.text for e in delays if e.created_at >= at("19:50")] == (
        ["Office: your 20:06 VL to HT train is delayed 8 min, arriving 21:14."] if late else []
    )


@pytest.mark.parametrize("zone_event", [False, True])
@pytest.mark.parametrize("remember", ["remember", "forget"])
async def test_1118_unknown_venlo_yes_follows_the_2006_home(tmp_path, zone_event, remember):
    rig = await rig_for(tmp_path)
    phone = await to_venlo(rig)
    (ask,) = [e for e in rig.rt.events_log if e.kind is EventKind.STATION_ASK]
    assert ask.text.endswith("are you taking the 20:06 from VL to HT?")
    await rig.run_until("19:52")
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    assert following(rig) == VENLO_2006
    assert rig.occ(KEY).lock.commitment.value == "committed"
    (prompt,) = [e for e in rig.rt.events_log if e.kind is EventKind.STATION_REMEMBER]

    async def answer() -> None:  # the remember question is still open after the Blerick fix
        assert await rig.rt.answer_prompt(prompt.facts.prompt_id, remember) is None

    await ride_home(rig, phone, zone_event, between=answer)
    assert_quiet_end(rig)
    kept = {"p1|return": ("VL",)} if remember == "remember" else {}
    assert rig.rt.state.remembered_stations == kept
    assert rig.rt.state.journey_records[KEY].train_arrival == at("21:06")


async def test_1118_known_venlo_delay_is_told_once_across_the_errand_timeout(tmp_path):
    """The provisional lock's errand timeout (19:50:52 + 45 min) comes after the 20:06 left
    Venlo: the lock is kept as it is, so a delay told before it is not told again after it,
    and a later change is still told."""
    rig = await rig_for(tmp_path, other="other_stations: [VL]")
    await to_venlo(rig)
    await rig.run_until("20:16")
    rig.provider.world = world(late_arr=12)
    await rig.run_until("20:38")
    assert following(rig) == VENLO_2006 and rig.occ(KEY).retained == ()
    rig.provider.world = world(late_arr=25)
    await rig.run_until("20:50")
    told = [
        e for e in rig.rt.events_log if e.kind is EventKind.DELAYED and e.created_at >= at("19:50")
    ]
    assert [e.text for e in told] == [
        "Office: your 20:06 VL to HT train leaves 0 min late and arrives 12 min late, arriving 21:18.",
        "Office: your 20:06 VL to HT train leaves 0 min late and arrives 25 min late, arriving 21:31.",
    ]
    assert {e.severity for e in told} == {Severity.SILENT}
    assert told[0].created_at < at("20:36") < told[1].created_at


async def test_1118_known_venlo_kept_lock_never_seen_boarding_ends_quietly(tmp_path):
    """The kept lock of a known station with no fix after the one near it: it is followed to its
    end and let go quietly there (never seen boarding, v0.2.6), not told as a lost journey."""
    rig = await rig_for(tmp_path, other="other_stations: [VL]")
    await to_venlo(rig)
    await rig.run_until("21:29")
    assert following(rig) == VENLO_2006
    await rig.run_until("23:59")
    assert loud_after(rig, "19:50") == []
    s = rig.occ(KEY)
    assert s is None or (s.lock is None and s.retained == ())
    assert rig.rt.state.journey_records[KEY].train_arrival == at("21:06")


@pytest.mark.parametrize("known", [True, False])
async def test_1118_blerick_fix_before_the_poll_sees_the_2006_leave(tmp_path, known):
    """The 20:06 leaves Venlo 7 min late, at 20:13, and the Blerick zone event lands before the
    next poll marks it left. The train's departure time says the user is on board: no reset."""
    rig = await rig_for(tmp_path, other="other_stations: [VL]" if known else "", late=7, late_arr=0)
    phone = await to_venlo(rig)
    if not known:
        (ask,) = [e for e in rig.rt.events_log if e.kind is EventKind.STATION_ASK]
        await rig.run_until("19:52")
        assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    await rig.run_until("20:12:30")
    await rig.run_until("20:14:30", poll=False)
    assert rig.occ(KEY).lock.legs[0].state.value == "boarding"  # not yet seen leaving
    rig.clock.set("20:14:37")
    await rig.rt.report_location("p1", LocationEvent("BR", "enter", at("20:14:37")))
    assert following(rig) == VENLO_2006
    beat(rig)
    await rig.run_until("21:09:30")
    await phone.fix(off(HT, 40, 300), "21:09:44", 31)
    await rig.run_until("22:00")
    assert loud_after(rig, "17:21:30") == []


async def test_1118_blerick_before_a_late_2006_leaves_venlo_takes_blerick(tmp_path):
    """The other side of the departure check: the 20:06 is still at Venlo, 20 min late, when the
    user turns up at Blerick. That is back at the usual station, so a train from there is taken."""
    rig = await rig_for(tmp_path, late=20, late_arr=20)
    await to_venlo(rig)
    (ask,) = [e for e in rig.rt.events_log if e.kind is EventKind.STATION_ASK]
    await rig.run_until("19:52")
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    assert following(rig) == VENLO_2006
    await rig.run_until("20:14:30")
    rig.clock.set("20:14:37")
    await rig.rt.report_location("p1", LocationEvent("BR", "enter", at("20:14:37")))
    s = rig.occ(KEY)
    assert s.boarding_station is None and s.lock.legs[0].board_station == "BR"

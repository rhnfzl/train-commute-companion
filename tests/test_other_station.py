"""Other boarding station. 2026-11-18 evening: after the 17:36 was let go, the user boarded the
20:06 at Venlo (VL here), not at the usual work station. The phone sent one fix about 400 m from
Venlo at 19:50, 16 min before departure, and the next fix was already on the train."""

from dataclasses import replace

import runtime_seam as seam
from builders import make_connection, make_leg
from runtime_rig import Rig, at
from test_never_boarded import KEY, PROFILE, evening, replay_to_departure

from train_commute_companion.location import NO_EVIDENCE, Evidence, Fix, LocationEvent, Zone
from train_commute_companion.model import Direction, DirectionState, EventKind, Severity
from train_commute_companion.view import in_station_circle, track_fix

RTD = (51.3700, 6.1500)
VL = (51.3640, 6.1720)
AAA = (51.6900, 5.2900)
NEAR_VL = (51.3676, 6.1720)  # about 400 m north of the station point
ROAD = (51.4000, 6.0500)  # nowhere near a station
WORK = (51.3800, 6.1300)
HOME = (51.7000, 5.3000)
PLACES = (Zone("work", *WORK, 120.0), Zone("home", *HOME, 120.0))
LOCATED = "{source: ha, entity: person.me}"


def venlo() -> list:
    out = []
    for dep, n in (("19:36", "6036"), ("20:06", "6066"), ("20:36", "6096")):
        arr = f"{int(dep[:2]) + 1}:{dep[3:]}"
        out.append(make_connection([make_leg("VL", "AAA", dep, arr, number=n)]))
    return out


async def rig_for(tmp_path, location: str = LOCATED, extra: str = "", other: str = "") -> Rig:
    rig = Rig(tmp_path, "15:40")
    text = PROFILE.replace("LOCATION", location) + extra
    if other:  # a return key, set inline
        text = text.replace('window: ["15:45", "21:30"]}', f'window: ["15:45", "21:30"], {other}}}')
        assert other in text
    rig.write(text=text)
    rig.provider.world = [*evening(), *venlo()]
    rig.provider.positions = {"RTD": RTD, "VL": VL, "AAA": AAA}
    await rig.rt.start()
    return rig


class Phone:
    """A location source: raw fixes through the shared `track_fix`, as every shell sends them."""

    def __init__(self, rig: Rig) -> None:
        self.rig, self.track = rig, None

    async def fix(
        self,
        where: tuple[float, float],
        hhmm: str,
        acc: float = 30,
        ev: Evidence = NO_EVIDENCE,
    ) -> list[tuple[str, str]]:
        rig = self.rig
        rig.clock.set(hhmm)
        view = rig.rt.view()
        p = next(p for p in view.profiles if p.id == "p1")
        epoch = view.location_epochs.get("p1", 0)
        got = track_fix(view, p, epoch, Fix(*where, at(hhmm), acc), self.track, PLACES)
        if got is None:
            return []
        self.track, events = got
        fix = Fix(*where, at(hhmm), acc)
        for zone, kind in events:
            here = replace(ev, in_station=in_station_circle(view, p, fix, zone))
            await rig.rt.report_location("p1", LocationEvent(zone, kind, at(hhmm), here))
        return [(z, k) for z, k in events]


def of(rig: Rig, *kinds: EventKind) -> list:
    return [e for e in rig.rt.events_log if e.kind in kinds]


def loud_after(rig: Rig, hhmm: str) -> list:
    return [
        (e.kind, e.text)
        for e in rig.rt.events_log
        if e.severity is Severity.LOUD and e.created_at > at(hhmm)
    ]


async def at_venlo(tmp_path, extra: str = "", location: str = LOCATED) -> tuple[Rig, Phone]:
    """The 11-18 evening up to the one fix near Venlo."""
    rig = await rig_for(tmp_path, location, extra)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    assert await phone.fix(ROAD, "19:48") == []  # a first fix only seeds the track
    assert await phone.fix(NEAR_VL, "19:50") == [("VL", "enter")]
    return rig, phone


async def test_1118_evening_venlo_single_far_fix_asks_then_follows_2006(tmp_path):
    rig, phone = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert ask.severity is Severity.SILENT
    assert ask.text.endswith("are you taking the 20:06 from VL to AAA?")
    assert ask.facts.choices == (("yes", "Yes"), ("other", "Other train"), ("no", "No"))
    pid = ask.facts.prompt_id
    assert len(pid) == 10 and all(ch in "0123456789abcdef" for ch in pid)
    s = rig.occ(KEY)
    assert s.lock is None and s.asked_stations == frozenset({"VL"})

    assert await rig.rt.answer_prompt(pid, "yes") is None
    s = rig.occ(KEY)
    assert s.boarding_station == "VL"
    assert s.lock is not None and s.lock.legs[0].train.train_number == "6066"
    assert s.lock.commitment.value == "committed"
    (remember,) = of(rig, EventKind.STATION_REMEMBER)
    assert remember.text.endswith("remember VL as a boarding station for your return trips?")
    assert remember.facts.choices == (("remember", "Yes"), ("forget", "No"))

    # once confirmed, the station has the usual radius: a fix that far out is a leave, harmless
    assert await phone.fix(NEAR_VL, "19:58") == [("VL", "leave")]
    await rig.run_until("21:20")
    assert await phone.fix(HOME, "21:20") == [("home", "enter")]
    await rig.run_until("22:00")  # arrived 21:06; the last watch ends about 21:46
    assert loud_after(rig, "19:49") == []
    assert rig.occ(KEY) is None or rig.occ(KEY).direction_state is DirectionState.DONE


async def test_unknown_station_ask_with_no_answer_does_nothing(tmp_path):
    rig, phone = await at_venlo(tmp_path)
    await phone.fix(ROAD, "19:53")
    assert await phone.fix(NEAR_VL, "19:55") == [("VL", "enter")]  # asked once per day
    await rig.run_until("21:31")
    assert len(of(rig, EventKind.STATION_ASK)) == 1
    assert loud_after(rig, "19:49") == []
    s = rig.occ(KEY)
    assert s is None or s.lock is None


async def test_no_closes_the_question(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "no") is None
    s = rig.occ(KEY)
    assert s.prompt is None and s.lock is None and s.boarding_station is None
    refused = await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    assert refused is not None and refused.text == "this question has expired"


async def test_unknown_answer_is_refused(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    refused = await rig.rt.answer_prompt(ask.facts.prompt_id, "maybe")
    assert refused is not None and refused.text == "unknown answer"


async def test_remember_flow_and_forget(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    profile_text = rig.path.read_bytes()
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    (remember,) = of(rig, EventKind.STATION_REMEMBER)
    assert await rig.rt.answer_prompt(remember.facts.prompt_id, "remember") is None
    assert rig.rt.state.remembered_stations == {"p1|return": ("VL",)}
    assert rig.rt.view().remembered == {"p1|return": ("VL",)}
    assert rig.occ(KEY).prompt is None
    assert rig.path.read_bytes() == profile_text  # never written to the profile file
    assert await rig.rt.forget_station("p1", Direction.RETURN, "VL") is None
    assert rig.rt.state.remembered_stations == {}
    refused = await rig.rt.forget_station("p1", Direction.RETURN, "VL")
    assert refused is not None


async def test_remember_no_answer_is_not_remembered(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    assert len(of(rig, EventKind.STATION_REMEMBER)) == 1  # never answered
    await rig.run_until("21:00")
    await rig.restart()
    await rig.run_until("22:00")
    assert rig.rt.state.remembered_stations == {}


async def test_remember_answer_no_is_not_remembered(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    (remember,) = of(rig, EventKind.STATION_REMEMBER)
    assert await rig.rt.answer_prompt(remember.facts.prompt_id, "forget") is None
    await rig.run_until("21:00")
    assert rig.rt.state.remembered_stations == {}


async def test_remember_prompt_can_be_turned_off(tmp_path):
    loc = "{source: ha, entity: person.me, ask_to_remember_station: false}"
    rig, _ = await at_venlo(tmp_path, location=loc)
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    assert rig.occ(KEY).lock is not None
    assert of(rig, EventKind.STATION_REMEMBER) == []


async def test_remembered_station_follows_without_asking(tmp_path):
    rig = await rig_for(tmp_path)
    st = rig.rt.state
    seam.seed_state(rig.rt, remembered_stations={**st.remembered_stations, "p1|return": ("VL",)})
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    assert of(rig, EventKind.STATION_ASK) == []
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.lock.legs[0].train.train_number == "6066"


async def test_known_station_auto_follows_with_one_message(tmp_path):
    rig = await rig_for(tmp_path, other="other_stations: [VL]")
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.lock.legs[0].train.train_number == "6066"
    # provisional until a fix inside the station circle: a walk past is let go quietly
    assert s.lock.source.value == "location" and s.lock.commitment.value == "provisional"
    assert not s.at_station
    (told,) = [e for e in of(rig, EventKind.AT_STATION) if e.created_at >= at("19:50")]
    assert told.severity is Severity.SILENT
    assert "at VL: following your 20:06 to AAA" in told.text
    await phone.fix(NEAR_VL, "19:53")
    assert len([e for e in of(rig, EventKind.AT_STATION) if e.created_at >= at("19:50")]) == 1
    assert of(rig, EventKind.STATION_ASK) == []


async def test_other_train_offers_choices_and_picks(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "other") is None
    picks = of(rig, EventKind.STATION_ASK)[1:]
    (pick,) = picks
    assert pick.text.endswith("which train from VL?")
    assert pick.facts.choices == (("t2006", "20:06"), ("t2036", "20:36"))
    await rig.run_until("19:56")  # no train is guessed while the user picks
    assert rig.occ(KEY).lock is None
    assert loud_after(rig, "19:49") == []
    assert await rig.rt.answer_prompt(pick.facts.prompt_id, "t2036") is None
    s = rig.occ(KEY)
    assert s.lock.legs[0].train.train_number == "6096" and s.prompt is None


async def test_driving_past_a_station_is_ignored(tmp_path):
    rig = await rig_for(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50", ev=Evidence(speed=13.0))
    assert of(rig, EventKind.STATION_ASK) == []
    assert rig.occ(KEY).asked_stations == frozenset()
    await phone.fix(ROAD, "19:51")
    road = Evidence(activity="Automotive", activity_at=at("19:51:30"))
    await phone.fix(NEAR_VL, "19:52", ev=road)
    assert of(rig, EventKind.STATION_ASK) == []


async def test_station_stop_while_riding_is_ignored(tmp_path):
    rig, phone = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    await rig.run_until("20:20")  # riding the 20:06
    tg = (51.5000, 5.8000)
    rig.provider.positions = {**rig.provider.positions, "TG": tg}
    calls = len(rig.provider.calls)
    assert await phone.fix((tg[0] + 0.001, tg[1]), "20:21") == [("VL", "leave")]
    assert of(rig, EventKind.STATION_ASK)[1:] == []
    assert rig.occ(KEY).boarding_station == "VL"
    assert all(q.origin != "TG" for q in rig.provider.calls[calls:])


async def test_fix_accuracy_above_radius_is_ignored(tmp_path):
    rig = await rig_for(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    assert await phone.fix(NEAR_VL, "19:50", acc=600) == []


async def test_fix_inside_work_near_station_is_ignored(tmp_path):
    rig = await rig_for(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    rig.provider.positions = {**rig.provider.positions, "WK": (WORK[0] + 0.002, WORK[1])}
    events = await phone.fix(WORK, "19:50")
    assert ("WK", "enter") not in events
    assert of(rig, EventKind.STATION_ASK) == []


async def test_dwell_needs_two_fixes(tmp_path):
    loc = "{source: ha, entity: person.me, other_station_dwell_min: 3}"
    rig, phone = await at_venlo(tmp_path, location=loc)
    assert of(rig, EventKind.STATION_ASK) == []
    assert await phone.fix(NEAR_VL, "19:53") == [("VL", "report")]
    assert len(of(rig, EventKind.STATION_ASK)) == 1


async def test_q12_miss_at_other_station(tmp_path):
    rig, phone = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    await rig.run_until("20:18")
    still = Evidence(activity="Stationary", activity_at=at("20:18"))
    await phone.fix(VL, "20:18", ev=still)  # still at Venlo 12 min after the 20:06 left
    s = rig.occ(KEY)
    assert s.lock.legs[-1].train.train_number == "6096"  # the 20:36 from Venlo
    assert s.lock.legs[-1].board_station == "VL"
    (miss,) = [e for e in of(rig, EventKind.MISSED_FIRST_TRAIN) if e.created_at > at("19:49")]
    assert miss.text.split(": ", 1)[1].startswith("Missed 20:06")


async def test_work_wifi_proves_no_miss_at_other_station(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    await rig.run_until("20:10")
    rig.clock.set("20:10")
    ev = Evidence(wifi=True)
    await rig.rt.report_location("p1", LocationEvent("work", "still", at("20:10"), ev))
    s = rig.occ(KEY)
    assert s.lock.legs[0].train.train_number == "6066"
    assert [e for e in of(rig, EventKind.MISSED_FIRST_TRAIN) if e.created_at > at("19:49")] == []


async def test_stale_ask_dropped_by_ledger(tmp_path):
    rig = await rig_for(tmp_path)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    rig.notifier.fails = lambda e: e.kind is EventKind.STATION_ASK
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    await phone.fix(NEAR_VL, "19:50")
    s = rig.occ(KEY)
    assert s.prompt is not None
    pid = s.prompt.id
    assert await rig.rt.answer_prompt(pid, "no") is None
    rig.notifier.fails = lambda e: False
    await rig.run_until("19:56")
    assert of(rig, EventKind.STATION_ASK) == []
    assert not any(o.event.kind is EventKind.STATION_ASK for o in rig.rt.state.obligations)


async def test_prompt_expired_after_the_trip_ends(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.run_until("21:35")
    refused = await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    assert refused is not None and refused.text == "this question has expired"


def test_state_round_trip_keeps_the_prompt():
    from lifecycle_fixtures import L1, L2, journey, occ

    from train_commute_companion.serde import decode, encode
    from train_commute_companion.state import OccurrenceState, StationPrompt

    prompt = StationPrompt("0123456789", "VL", "ask", L1.train, (("yes", "Yes"),), at("19:50"))
    s = replace(
        occ(journey([L1, L2])),
        boarding_station="VL",
        prompt=prompt,
        asked_stations=frozenset({"VL"}),
        station_seen={"VL": at("19:50")},
    )
    assert decode(OccurrenceState, encode(s)) == s


async def test_back_at_the_usual_station_follows_a_train_from_there(tmp_path):
    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    await rig.rt.answer_prompt(ask.facts.prompt_id, "yes")
    await rig.run_until("20:20")
    rig.clock.set("20:20")  # the 20:06 from Venlo left; the user turns up at the usual station
    await rig.rt.report_location("p1", LocationEvent("RTD", "enter", at("20:20")))
    s = rig.occ(KEY)
    assert s.boarding_station == "VL"  # riding from Venlo through the usual station

    (tmp_path / "b").mkdir()
    rig2, _ = await at_venlo(tmp_path / "b")
    (ask,) = of(rig2, EventKind.STATION_ASK)
    await rig2.rt.answer_prompt(ask.facts.prompt_id, "yes")
    rig2.clock.set("19:55")
    await rig2.rt.report_location("p1", LocationEvent("RTD", "enter", at("19:55")))
    s = rig2.occ(KEY)
    assert s.boarding_station is None
    assert s.lock.legs[0].board_station == "RTD" and s.lock.legs[0].train.train_number == "3576"


async def test_auto_lock_counts_as_unfollowed_but_a_known_station_then_asks(tmp_path):
    """TCC's own guess is no train the user follows, so the station is noticed. A known station
    is then asked about, not followed by itself: the guess may still be the train they take."""
    rig = await rig_for(tmp_path, other="other_stations: [VL]")
    await rig.run_until("16:20")
    s = rig.occ(KEY)
    assert s.lock.source.value == "auto" and s.lock.commitment.value != "committed"
    phone = Phone(rig)
    await phone.fix(ROAD, "16:20")
    assert await phone.fix(NEAR_VL, "16:22") == [("VL", "enter")]
    s = rig.occ(KEY)
    assert s.boarding_station is None and s.lock.source.value == "auto"
    assert s.lock.legs[0].board_station == "RTD"
    (ask,) = of(rig, EventKind.STATION_ASK)
    assert ask.text.endswith("are you taking the 19:36 from VL to AAA?")
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "yes") is None
    s = rig.occ(KEY)
    assert s.boarding_station == "VL" and s.lock.legs[0].train.train_number == "6036"
    assert of(rig, EventKind.STATION_REMEMBER) == []  # already a known station


async def test_committed_lock_counts_as_followed(tmp_path):
    rig = await rig_for(tmp_path, other="other_stations: [VL]")
    await rig.run_until("16:20")
    assert await rig.rt.leaving("p1") is None  # the tap commits the 16:36
    phone = Phone(rig)
    await phone.fix(ROAD, "16:20")
    calls = len(rig.provider.calls)
    assert await phone.fix(NEAR_VL, "16:22") == []  # no zone: nothing is sent or asked
    s = rig.occ(KEY)
    assert s.boarding_station is None and s.lock.legs[0].board_station == "RTD"
    assert all(q.origin != "VL" for q in rig.provider.calls[calls:])


async def test_other_train_keeps_the_question_open_when_ns_fails(tmp_path):
    from train_commute_companion.providers.base import ProviderError

    rig, _ = await at_venlo(tmp_path)
    (ask,) = of(rig, EventKind.STATION_ASK)
    rig.provider.error = lambda q: ProviderError("down") if q.origin == "VL" else None
    refused = await rig.rt.answer_prompt(ask.facts.prompt_id, "other")
    assert refused is not None and "try again" in refused.text
    s = rig.occ(KEY)
    assert s.prompt is not None and s.prompt.id == ask.facts.prompt_id
    assert s.boarding_station is None
    rig.provider.error = lambda q: None
    assert await rig.rt.answer_prompt(ask.facts.prompt_id, "other") is None
    assert rig.occ(KEY).prompt.stage == "pick"

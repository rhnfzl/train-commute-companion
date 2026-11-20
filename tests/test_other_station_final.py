"""Other boarding station and window mode: the cases the final 0.3.0 review found. Each one
failed before its fix."""

from builders import make_connection, make_leg
from runtime_rig import at
from test_never_boarded import KEY, replay_to_departure
from test_other_station import NEAR_VL, ROAD, Phone, of
from test_other_station import rig_for as station_rig

from train_commute_companion.model import EventKind

S2 = (51.55, 5.95)


def _s2_trains(rig) -> None:
    rig.provider.positions = {**rig.provider.positions, "S2": S2}
    for dep, n in (("20:20", "1"), ("20:40", "2")):
        rig.provider.world.append(make_connection([make_leg("S2", "AAA", dep, "21:30", number=n)]))


# --- the 30-minute guard survives leaving a station circle ---


async def test_leaving_a_known_station_keeps_the_30_minute_guard(tmp_path):
    rig = await station_rig(tmp_path, other="other_stations: [VL]")
    _s2_trains(rig)
    await replay_to_departure(rig)
    await rig.run_until("19:48")
    phone = Phone(rig)
    await phone.fix(ROAD, "19:48")
    assert await phone.fix(NEAR_VL, "19:50") == [("VL", "enter")]
    assert rig.occ(KEY).lock is not None  # the provisional VL guess
    assert ("VL", "leave") in await phone.fix(ROAD, "19:53")
    await phone.fix((S2[0] + 0.001, S2[1]), "19:55", acc=20)
    assert of(rig, EventKind.STATION_ASK) == []
    assert at("19:50") == rig.occ(KEY).station_visits["VL"]

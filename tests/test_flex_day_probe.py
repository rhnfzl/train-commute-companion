"""Window mode and day overrides on the runtime rig: a line problem told under pick on_leave
before any train is followed (Q11), and a day override that switches the mode for one day."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from builders import make_connection, make_leg
from runtime_rig import DAY, Rig, at
from test_disruption_train_type import incident

from train_commute_companion.model import EventKind

# --- Q11 under pick: on_leave ---

Q11_PROFILE = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon, tue, wed, thu, fri]
    home_station: "HT"
    work_station: "EHV"
    outbound: {mode: window, window: ["07:00", "10:00"], buffer_min: 10, pick: on_leave}
    return: {first_train: "17:52", buffer_min: 5}
    location: {source: ha, entity: person.me}
"""


def intercities() -> list:
    """'s-Hertogenbosch to Eindhoven through Boxtel, Intercity only, every half hour."""
    out = []
    for i, (h, m) in enumerate((h, m) for h in range(7, 10) for m in (0, 30)):
        leg = make_leg("HT", "EHV", f"{h:02d}:{m:02d}", f"{h:02d}:{m + 20:02d}",
                       number=f"35{i:02d}", stops=("BTL",))  # fmt: skip
        out.append(make_connection([replace(leg, category="IC")]))
    return out


async def test_on_leave_tells_a_sprinter_only_notice_before_any_train_is_followed(tmp_path):
    """Q11: a notice that hits only one train type is judged by the followed train's type. Under
    on_leave no train is followed before leaving, so unsure: it is sent, and delivered."""
    rig = Rig(tmp_path, "06:30")
    rig.write(text=Q11_PROFILE)
    rig.provider.world = intercities()
    rig.provider.disruptions = incident("geen Sprinters", "geen Sprinters")
    await rig.rt.start()
    await rig.run_until("07:30")
    s = rig.occ()
    assert s is not None and s.lock is None and not s.travelling
    sent = [e for e in rig.notifier.attempts if e.kind is EventKind.DISRUPTION]
    assert len(sent) == 1
    assert "geen Sprinters" in sent[0].text and "Until about 16:45" in sent[0].text


# --- a day override switches the mode at runtime ---

SAT = DAY + timedelta(days=4)  # 2026-10-31
THU = DAY + timedelta(days=2)  # 2026-10-29

DAY_PROFILE = """
profiles:
  - id: "p1"
    name: "office"
    days: [mon, tue, wed, thu, fri, sat]
    home_station: "AAA"
    work_station: "RTD"
    outbound: OUT
    return: {first_train: "17:52", buffer_min: 5}
"""
WINDOW_SAT_FIXED = (
    '{mode: window, window: ["07:00", "10:00"], buffer_min: 10, '
    'first_train_by_weekday: {sat: "08:45"}, by_weekday: {sat: {mode: fixed}}}'
)
FIXED_THU_WINDOW = (
    '{first_train: "08:12", buffer_min: 10, '
    'by_weekday: {thu: {mode: window, window: ["07:00", "10:00"]}}}'
)


def mornings(day: date) -> list:
    """AAA to RTD at 07:00, 07:30, 08:12, 08:45 and 09:15 on `day`, 38 min each."""
    out = []
    for i, dep in enumerate(("07:00", "07:30", "08:12", "08:45", "09:15")):
        h, m = int(dep[:2]), int(dep[3:]) + 38
        arr = f"{h + m // 60:02d}:{m % 60:02d}"
        leg = make_leg("AAA", "RTD", dep, arr, number=f"9{i}{day.day}", day=day)
        out.append(make_connection([leg]))
    return out


async def day_rig(tmp_path, out: str, day: date) -> Rig:
    rig = Rig(tmp_path, "06:00")
    rig.clock.t = at("06:00", day)
    rig.write(text=DAY_PROFILE.replace("OUT", out))
    rig.provider.world = mornings(day)
    await rig.rt.start()
    return rig


def followed(rig: Rig, day: date) -> str | None:
    s = rig.occ(f"p1|outbound|{day.isoformat()}")
    return (
        None if s is None or s.lock is None else f"{s.lock.legs[0].train.planned_departure:%H:%M}"
    )


@pytest.mark.parametrize(
    ("out", "day", "before", "after", "train"),
    [
        (WINDOW_SAT_FIXED, DAY, "06:33", "06:38", "07:00"),  # base window day: the earliest
        (WINDOW_SAT_FIXED, SAT, "07:43", "07:48", "08:45"),  # fixed Saturday: its first train
        (FIXED_THU_WINDOW, DAY, "07:10", "07:15", "08:12"),  # base fixed day: the usual train
        (FIXED_THU_WINDOW, THU, "06:33", "06:38", "07:00"),  # window Thursday: the earliest
    ],
)
async def test_a_day_override_runs_its_own_mode(tmp_path, out, day, before, after, train):
    """The world has an earlier train in the window on every day, so the two modes differ. A
    window day opens buffer + 15 min before its start; a fixed day opens 60 min before its
    first train (the default watch window) and follows that train, not the earliest."""
    rig = await day_rig(tmp_path, out, day)
    await rig.run_until(before)
    assert followed(rig, day) is None
    await rig.run_until(after)
    assert followed(rig, day) == train
    (shown,) = [o for o in rig.rt.view().occurrences if o.key.service_date == day]
    if train == "07:00":
        assert shown.usual_departure is None
    else:
        assert shown.usual_departure is not None
        assert f"{shown.usual_departure:%H:%M}" == train

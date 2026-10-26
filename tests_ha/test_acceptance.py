"""Release acceptance through Home Assistant (spec 10.4): the owner replays, reload, restart,
adoption and `all_good` run through a real config entry, the host thread and the notifier."""

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import harness
import pytest
from harness import (
    AMS,
    PROFILE,
    PROFILES,
    STATIONS,
    Harness,
    ReplayStations,
    drive,
    jump,
    prepare,
    rebound,
    setup_tcc,
    write_profiles,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from test_runtime import usual_on

from custom_components.train_commute_companion import validate
from custom_components.train_commute_companion.const import DOMAIN
from train_commute_companion.lookahead import BaselineHistory, BaselineVersion
from train_commute_companion.model import Signature, SignatureLeg
from train_commute_companion.profile import parse_profiles
from train_commute_companion.replay import Replay, apply_event, load_replay

pytestmark = pytest.mark.usefixtures("tcc_freezer")

ROOT = Path(__file__).resolve().parents[1]
REPLAYS = ROOT / "tests" / "replays"
LINE = re.compile(r"(\d\d:\d\d) (LOUD|SILENT) \w+: (.*)")


@dataclass
class Want:
    severity: str
    text: str


def expected_lines(path: Path) -> list[Want]:
    """The `.out` file's alerts, without its closing attribution line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Want(m[2], m[3]) for x in lines if (m := LINE.fullmatch(x))]


def sev(call) -> str:
    return (
        "LOUD" if call.data["data"]["push"]["interruption-level"] == "time-sensitive" else "SILENT"
    )


def codes(text: str) -> frozenset[str]:
    cfg, errors = parse_profiles(text, None)
    assert not errors
    return frozenset(
        c
        for p in cfg
        for c in (p.home_station, p.work_station, *p.outbound.transfers, *p.return_.transfers)
    )


async def setup_from_replay(hass, path: Path) -> tuple[Harness, Replay]:
    rp = load_replay(path)
    text = rp.profile_path.read_text(encoding="utf-8")
    entry, clock, provider, pushes = prepare(
        hass, text, "00:00", (), replay=(rp.clock, ReplayStations(rp.provider, codes(text)))
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    h = Harness(entry, entry.runtime_data.host, clock, provider, pushes)
    await h.host.run(lambda rt: rt.start())
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    return h, rp


async def drive_replay(h: Harness, rp: Replay) -> None:
    """`replay_lines`' loop: events, a poll on even minutes, the watchdog, 30 s steps."""
    clock, pending = rp.clock, list(rp.timeline)
    coordinator = h.entry.runtime_data.coordinator
    while clock.now() < rp.end:
        while pending and pending[0][0] <= clock.now():
            event, now = pending.pop(0)[1], clock.now()
            await h.host.run(lambda rt, e=event, n=now: apply_event(rt, e, n))
        now = clock.now()
        if now.minute % 2 == 0 and now.second == 0:
            await h.host.run(lambda rt: rt.poll_once())
        await h.host.run(lambda rt: rt.watchdog_once())
        await clock.sleep(30)
        harness.FREEZER.move_to(clock.now())
        await coordinator.async_refresh()
    await h.hass.async_block_till_done()


@pytest.mark.parametrize(
    "name", ["direct", "one_transfer", "cancelled_second_leg", "transfer_broken_by_delay"]
)
async def test_replay_through_ha_delivers_every_alert_loud_or_silent(hass, name):
    h, rp = await setup_from_replay(hass, REPLAYS / f"{name}.yaml")
    await drive_replay(h, rp)
    want = expected_lines(REPLAYS / f"{name}.out")
    assert want
    assert [(sev(c), c.data["message"]) for c in h.pushes] == [(w.severity, w.text) for w in want]


async def test_starter_file_runs_in_ha(hass):
    starter = (ROOT / "examples" / "profiles.yaml").read_text(encoding="utf-8")
    h = await setup_tcc(hass, starter)
    path = h.entry.options["profiles_path"]
    assert await validate.validate_file(hass, path, STATIONS) == []
    assert hass.states.get("sensor.office_a_outbound_status") is not None
    assert hass.states.get("sensor.office_b_outbound_status") is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "profile_refused") is None
    assert not [c for c in h.pushes if sev(c) == "LOUD" and "refused" in c.data["message"]]


async def test_all_good_sent_and_withheld_through_ha(hass):
    h = await setup_tcc(hass, PROFILE, world=[usual_on(date(2026, 10, 27))])
    await drive(h, "07:54")
    good = [c for c in h.pushes if "is on time per NS" in c.data["message"]]
    assert len(good) == 1 and sev(good[0]) == "SILENT"
    # the default world has the second leg late: an at-risk answer says nothing good
    h = await setup_tcc(hass, PROFILE)
    await drive(h, "07:54")
    assert h.pushes and not [c for c in h.pushes if "is on time per NS" in c.data["message"]]


def baseline(first: time, effective: date) -> BaselineHistory:
    sig = Signature(
        (SignatureLeg("AAA", "UT", first, 0, 28), SignatureLeg("UT", "RTD", time(8, 50), 0, 34))
    )
    return BaselineHistory((BaselineVersion(1, sig, effective, True),))


async def test_adoption_carried_into_the_next_day_through_ha(hass):
    one = (PROFILES / "commute_tue_to_thu.yaml").read_text()
    h = await setup_tcc(hass, one, world=[], start=False)
    await jump(h, "2026-10-26 05:50")  # before any occurrence exists
    await h.host.run(lambda rt: rt.start())
    days = [date(2026, 10, 27) + timedelta(days=i) for i in range(12)]
    h.provider.world = [usual_on(d, "08:15") for d in days]

    def seed(rt):
        rt._st = replace(
            rt.state, baselines={"p1|outbound": baseline(time(8, 12), date(2026, 10, 1))}
        )

    await h.host.run(seed)
    await jump(h, "2026-10-26 05:55")
    await h.host.run(lambda rt: rt.lookahead_once())
    await h.entry.runtime_data.coordinator.async_refresh()
    told = [c for c in h.pushes if "NS moved your 08:12 to 08:15" in c.data["message"]]
    assert len(told) == 1 and sev(told[0]) == "SILENT"
    # a restart: the adopted baseline lives in the store, not in the profile file
    assert await hass.config_entries.async_reload(h.entry.entry_id)
    h = await rebound(h)
    h.provider.world = [usual_on(d, "08:15") for d in days]
    seen: list[datetime] = []
    h.provider.hook = lambda q: seen.append(h.clock.now())
    await jump(h, "2026-10-27 06:50")
    await drive(h, "08:20")
    assert seen and min(seen) >= datetime(2026, 10, 27, 7, 15, tzinfo=AMS)  # not the old 07:12
    state = hass.states.get("sensor.office_a_outbound_lock")
    assert state.attributes["trains"][0].startswith("08:15 AAA-UT")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "moved_p1_outbound")
    assert issue is not None
    assert Path(h.entry.options["profiles_path"]).read_text() == one  # the file is never rewritten


async def reload_file(h: Harness) -> None:
    await h.host.run(lambda rt: rt.check_file())
    await h.entry.runtime_data.coordinator.async_refresh()
    await h.hass.async_block_till_done()


def refusals(h: Harness) -> list:
    return [c for c in h.pushes if "profile change refused" in c.data["message"]]


async def revision(h: Harness) -> int:
    return await h.host.run(lambda rt: rt.config.revision)


async def test_yaml_reload_valid_invalid_overlap(hass):
    h = await setup_tcc(hass, PROFILE)
    first = await revision(h)
    before = len(h.pushes)
    write_profiles(hass, (PROFILES / "office_a_edited.yaml").read_text())
    await reload_file(h)
    assert await revision(h) == first + 1 and len(h.pushes) == before
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "profile_refused") is None
    good = await revision(h)
    for name in ("bad_starter.yaml", "both_on_wed.yaml"):
        write_profiles(hass, (PROFILES / name).read_text())
        await reload_file(h)
        assert await revision(h) == good, name  # the last valid configuration keeps running
        assert issues.async_get_issue(DOMAIN, "profile_refused") is not None, name
        assert (
            sev(h.pushes[-1]) == "LOUD" and "profile change refused" in h.pushes[-1].data["message"]
        )
        assert hass.states.get("sensor.office_a_outbound_status") is not None
    assert len(refusals(h)) == 2


@pytest.mark.parametrize("damage", ["missing", "bad"])
async def test_restart_with_missing_or_bad_file_runs_stored_config(hass, damage):
    h = await setup_tcc(hass, PROFILE)
    path = Path(h.entry.options["profiles_path"])
    assert await hass.config_entries.async_unload(h.entry.entry_id)
    if damage == "missing":
        path.unlink()
    else:
        path.write_text((PROFILES / "bad_starter.yaml").read_text())
    assert await hass.config_entries.async_setup(h.entry.entry_id)
    assert h.entry.state is ConfigEntryState.LOADED
    h = await rebound(h)
    assert hass.states.get("sensor.office_a_outbound_status") is not None
    assert await h.host.run(lambda rt: [p.id for p in rt.config.profiles]) == ["p1", "p2"]
    assert len(refusals(h)) == 1 and sev(refusals(h)[0]) == "LOUD"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "profile_refused") is not None
    await drive(h, "07:54")
    assert hass.states.get("sensor.office_a_outbound_status") is not None

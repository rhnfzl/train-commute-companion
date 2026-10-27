"""Release acceptance through the Docker shell (spec 10.4, Docker and Telegram rows): the owner
replays, reload, restart, adoption, every action and both location sources run through the
runtime, the Bot, the Follower and the TelegramNotifier against a fake Bot API."""

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from builders import AMS, ONE_TRANSFER, only_occurrence
from conftest import (
    CHAT,
    TOKEN,
    FakeTelegram,
    StationsProvider,
    TickClock,
    build_from_replay,
    drive_replay,
    expected_lines,
    spied,
)
from tcc_app.main import App, Settings, build, health_report, start_web
from tcc_app.owntracks import handler
from test_follow import FAR, UT, located_app
from test_main import CODES, make_app
from test_owntracks import SECRET, basic, post
from test_runtime import PROFILES_DIR, Provider, at, bump_mtime, usual_on

from train_commute_companion.lookahead import BaselineHistory, BaselineVersion
from train_commute_companion.model import Severity, Signature, SignatureLeg

REPLAYS = Path(__file__).resolve().parents[1] / "tests" / "replays"
OUT = "p1|outbound|2026-10-27"


def silent(m: dict[str, Any]) -> bool:
    return bool(m["disable_notification"])


def msg(app: App, text: str, uid: int = 1) -> dict[str, Any]:
    body = {
        "message_id": uid,
        "date": int(app.rt.now().timestamp()),
        "chat": {"id": CHAT},
        "text": text,
    }
    return {"update_id": uid, "message": body}


def tap(data: str, uid: int = 1) -> dict[str, Any]:
    message = {"message_id": 1, "date": 0, "chat": {"id": CHAT}}
    return {"update_id": uid, "callback_query": {"id": f"q{uid}", "data": data, "message": message}}


async def run_until(app: App, hhmm: str, step: int = 30) -> None:
    """The replay loop on the app's own clock: a poll on even minutes, the watchdog."""
    clock = app.rt._clock  # type: ignore[attr-defined]
    end = at(hhmm) if len(hhmm) == 5 else datetime.fromisoformat(hhmm).replace(tzinfo=AMS)
    while clock.now() < end:
        now = clock.now()
        if now.minute % 2 == 0 and now.second == 0:
            await app.rt.poll_once()
        await app.rt.watchdog_once()
        await clock.sleep(step)


@pytest.mark.parametrize(
    "name",
    ["direct", "one_transfer", "two_transfers", "cancelled_second_leg", "transfer_broken_by_delay"],
)
async def test_replay_through_telegram_delivers_every_alert_loud_or_silent(tmp_path, tg, name):
    app, rp = build_from_replay(tmp_path, tg, REPLAYS / f"{name}.yaml")
    await drive_replay(app, rp)
    want = expected_lines(REPLAYS / f"{name}.out")
    assert want
    assert [(silent(m), m["text"]) for m in tg.sent] == [(s is Severity.SILENT, t) for s, t in want]


def paired(tmp_path: Path, tg: FakeTelegram, world: list, codes: frozenset[str] = CODES) -> App:
    tmp_path.mkdir(exist_ok=True)
    clock = TickClock(at("07:50"))
    return make_app(tmp_path, tg, clock, StationsProvider(Provider(clock, world), codes))


async def test_all_good_sent_and_withheld_through_telegram(tmp_path, tg):
    app = paired(tmp_path, tg, [usual_on(date(2026, 10, 27))])
    await app.rt.start()
    await run_until(app, "07:54")
    good = [m for m in tg.sent if "is on time per NS" in m["text"]]
    assert len(good) == 1 and silent(good[0])
    # the default world has the second leg late: an at-risk answer says nothing good
    tg.sent.clear()
    app = paired(tmp_path / "b", tg, list(ONE_TRANSFER))
    await app.rt.start()
    await run_until(app, "07:54")
    assert tg.sent and not [m for m in tg.sent if "is on time per NS" in m["text"]]


def baseline(first: time, effective: date) -> BaselineHistory:
    sig = Signature(
        (SignatureLeg("AAA", "UT", first, 0, 28), SignatureLeg("UT", "RTD", time(8, 50), 0, 34))
    )
    return BaselineHistory((BaselineVersion(1, sig, effective, True),))


def build_at(tmp_path: Path, tg: FakeTelegram, clock: TickClock, provider: Provider) -> App:
    s = Settings("k", TOKEN, CHAT, None, tmp_path / "profiles.yaml", tmp_path)
    return build(s, clock=clock, provider=StationsProvider(provider, CODES), client=tg.client())


async def test_adoption_carried_into_the_next_day_through_telegram(tmp_path, tg):
    one = (PROFILES_DIR / "commute_tue_to_thu.yaml").read_text()
    (tmp_path / "profiles.yaml").write_text(one)
    clock = TickClock(datetime(2026, 10, 26, 5, 50, tzinfo=AMS))
    days = [date(2026, 10, 27) + timedelta(days=i) for i in range(12)]
    provider = Provider(clock, [usual_on(d, "08:15") for d in days])
    app = build_at(tmp_path, tg, clock, provider)
    await app.rt.start()
    app.rt._st = replace(
        app.rt.state, baselines={"p1|outbound": baseline(time(8, 12), date(2026, 10, 1))}
    )
    clock.t = datetime(2026, 10, 26, 5, 55, tzinfo=AMS)
    await app.rt.lookahead_once()
    told = [m for m in tg.sent if "NS moved your 08:12 to 08:15" in m["text"]]
    assert len(told) == 1 and silent(told[0])
    # a restart: the adopted baseline lives in the store, not in the profile file
    seen: list[datetime] = []
    provider.hook = lambda q: seen.append(clock.now())
    clock.t = datetime(2026, 10, 27, 6, 50, tzinfo=AMS)
    app = build_at(tmp_path, tg, clock, provider)
    await app.rt.start()
    await run_until(app, "08:20")
    assert seen and min(seen) >= at("07:15")  # not the old 07:12
    assert app.rt.state.baselines["p1|outbound"] is not None
    await app.bot.handle(msg(app, "/status"))
    status = tg.sent[-1]["text"]
    assert "08:15 AAA to UT" in status and "NS moved your 08:12 to 08:15" in status
    assert (tmp_path / "profiles.yaml").read_text() == one  # the file is never rewritten


def refusals(tg: FakeTelegram) -> list[dict[str, Any]]:
    return [m for m in tg.sent if "profile change refused" in m["text"]]


async def test_yaml_reload_valid_invalid_overlap(tmp_path, tg):
    app = paired(tmp_path, tg, list(ONE_TRANSFER), CODES | {"BBB"})
    path = app.settings.profiles
    path.write_text((PROFILES_DIR / "office_a.yaml").read_text())
    await app.rt.start()
    first, before = app.rt.config.revision, len(tg.sent)
    path.write_text((PROFILES_DIR / "office_a_edited.yaml").read_text())
    bump_mtime(path)
    await app.rt.check_file()
    assert app.rt.config.revision == first + 1 and len(tg.sent) == before
    good = app.rt.config.revision
    for i, name in enumerate(("bad_starter.yaml", "both_on_wed.yaml"), 1):
        path.write_text((PROFILES_DIR / name).read_text())
        bump_mtime(path)
        await app.rt.check_file()
        assert app.rt.config.revision == good, name  # the last valid configuration keeps running
        assert len(refusals(tg)) == i and not silent(refusals(tg)[-1]), name
        await app.bot.handle(msg(app, "/status", i))
        assert "Profile file refused" in tg.sent[-1]["text"], name
        _, body = health_report(app.rt.view(), True, True)
        assert body["profile_refused"], name


@pytest.mark.parametrize("damage", ["missing", "bad"])
async def test_restart_with_missing_or_bad_file_runs_stored_config(tmp_path, tg, damage):
    app = paired(tmp_path, tg, list(ONE_TRANSFER))
    await app.rt.start()
    ids = [p.id for p in app.rt.config.profiles]
    path = app.settings.profiles
    again = paired(tmp_path, tg, list(ONE_TRANSFER))
    if damage == "missing":
        path.unlink(missing_ok=True)
    else:
        path.write_text((PROFILES_DIR / "bad_starter.yaml").read_text())
    await again.rt.start()
    assert [p.id for p in again.rt.config.profiles] == ids
    assert len(refusals(tg)) == 1 and not silent(refusals(tg)[0])


async def test_every_telegram_action(tmp_path, tg):
    app = paired(tmp_path, tg, list(ONE_TRANSFER))
    await app.rt.start()
    await app.rt.poll_once()

    def texts() -> list[str]:  # replies only: a delay alert may come with the poll
        return [m["text"] for m in tg.sent if not m["text"].startswith("Office A:")]

    await app.bot.handle(msg(app, "/status"))
    assert texts()[-1].endswith("Data: NS") and "reply_markup" in tg.sent[-1]
    tg.sent.clear()
    await app.bot.handle(msg(app, "/choose 08:44", 2))
    assert only_occurrence(app.rt).lock.legs[0].train.planned_departure == at("08:44")
    await app.bot.handle(msg(app, "/leaving", 3))
    await app.bot.handle(msg(app, "/skip outbound", 4))
    assert app.rt.state.occurrences[OUT].not_travelling
    assert texts() == [
        "Locked the 08:44 for Office A.",
        "Marked as leaving now.",
        "Not travelling: alerts for that journey stop.",
    ]
    tg.sent.clear()
    app = paired(tmp_path / "b", tg, list(ONE_TRANSFER))
    await app.rt.start()
    await app.rt.poll_once()
    await app.bot.handle(tap("l|p1|outbound|2026-10-27", 1))
    await app.bot.handle(tap("c|p1|outbound|2026-10-27|08:44|1236", 2))
    await app.bot.handle(tap("s|p1|outbound|2026-10-27", 3))
    assert tg.answered == ["q1", "q2", "q3"]
    assert texts() == [
        "Marked as leaving now.",
        "Locked the 08:44 for Office A.",
        "Not travelling: alerts for that journey stop.",
    ]


def location_update(hhmm: str, lat: float, lon: float, uid: int) -> dict[str, Any]:
    body = {"message_id": 1, "chat": {"id": CHAT}, "date": int(at("08:20").timestamp()),
            "edit_date": int(at(hhmm).timestamp()),
            "location": {"latitude": lat, "longitude": lon}}  # fmt: skip
    return {"update_id": uid, "edited_message": body}


async def test_both_location_sources_produce_a_zone_event(tmp_path, tg):
    app = await located_app(tmp_path, tg, "located_via_ut_telegram")
    try:
        spied(app)
        for i, (hhmm, pos) in enumerate((("08:20", FAR), ("08:24", UT)), 1):
            app.clock.t = at(hhmm)  # type: ignore[attr-defined]
            await app.bot.handle(location_update(hhmm, *pos, i))
            await app.follower.drain()
        assert "UT" in only_occurrence(app.rt).zone_reports
    finally:
        await app.follower.close()
    other = tmp_path / "ot"
    other.mkdir()
    app = await located_app(other, tg, "located_via_ut_owntracks", now="07:40")
    spied(app)
    server = await start_web(0, health=lambda: (200, {}), owntracks=handler(app.follower),
                             secret=SECRET, host="127.0.0.1")  # fmt: skip
    port = server.sockets[0].getsockname()[1]
    try:
        for hhmm, pos in (("07:41", FAR), ("07:42", UT)):
            app.clock.t = at(hhmm)  # type: ignore[attr-defined]
            body = {
                "_type": "location",
                "lat": pos[0],
                "lon": pos[1],
                "tst": int(at(hhmm).timestamp()),
            }
            assert (await post(port, json.dumps(body).encode(), basic("u", SECRET)))[0] == 200
            await app.follower.drain()
        assert "UT" in only_occurrence(app.rt).zone_reports
    finally:
        server.close()
        await server.wait_closed()
        await app.follower.close()


async def test_restart_sends_no_delivered_alert_again(tmp_path, tg):
    clock = TickClock(at("07:50"))
    provider = StationsProvider(Provider(clock, [usual_on(date(2026, 10, 27))]), CODES)
    app = make_app(tmp_path, tg, clock, provider)
    await app.rt.start()
    await run_until(app, "08:14")
    sent = list(tg.sent)
    assert sent
    again = make_app(tmp_path, tg, clock, provider)
    await again.rt.start()
    await again.rt.poll_once()
    await again.rt.watchdog_once()
    assert tg.sent == sent

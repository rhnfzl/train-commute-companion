import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from builders import AMS, ONE_TRANSFER, only_occurrence
from conftest import CHAT, TOKEN, StationsProvider, TickClock, feed, spied
from tcc_app.follow import Fix
from tcc_app.main import App, Settings, build
from test_runtime import PROFILES_DIR, Provider, at, bump_mtime

from train_commute_companion.lifecycle import Commitment, LockSource

CODES = frozenset({"AAA", "UT", "RTD"})
POSITIONS = {"AAA": (52.3, 4.9), "UT": (52.1, 5.1), "RTD": (51.0, 4.0)}
FAR = (53.0, 6.0)
UT = (52.1, 5.1)


async def located_app(
    tmp_path: Path, tg, name: str, positions=POSITIONS, now: str = "08:00"
) -> App:
    clock = TickClock(at(now))
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text((PROFILES_DIR / f"{name}.yaml").read_text())
    s = Settings("k", TOKEN, CHAT, None, profiles, tmp_path)
    provider = StationsProvider(Provider(clock, list(ONE_TRANSFER)), CODES, positions)
    app = build(s, clock=clock, provider=provider, client=tg.client())
    app.clock = clock  # type: ignore[attr-defined]
    await app.rt.start()
    return app


@pytest.fixture
async def app(tmp_path, tg):
    a = await located_app(tmp_path, tg, "located_via_ut_telegram")
    yield a
    await a.follower.close()


@pytest.fixture
async def app_owntracks(tmp_path, tg):
    a = await located_app(tmp_path, tg, "located_via_ut_owntracks", now="07:40")
    yield a
    await a.follower.close()


async def switch(app: App, *names: str) -> None:
    path = app.settings.profiles
    for name in names:
        path.write_text((PROFILES_DIR / f"{name}.yaml").read_text())
        bump_mtime(path)
        await app.rt.check_file()


async def test_first_fix_seeds_then_enter_and_leave_reach_the_core(app):
    await feed(app, *FAR, "08:20")
    await feed(app, *UT, "08:24")
    await feed(app, *FAR, "08:30")
    assert spied(app) == [("UT", "enter"), ("UT", "leave")]
    assert "UT" in only_occurrence(app.rt).zone_reports  # 10.4: Telegram produces a zone event


async def test_report_throttled_to_one_per_zone_per_2_min(app):
    await feed(app, *FAR, "08:29")
    await feed(app, *UT, "08:30")
    await feed(app, 52.10001, 5.1, "08:31")
    assert spied(app) == [("UT", "enter")]
    await feed(app, 52.10002, 5.1, "08:32:30")
    assert spied(app) == [("UT", "enter"), ("UT", "report")]


async def test_first_fix_inside_a_zone_only_seeds_and_starts_the_report_timer(app):
    await feed(app, *UT, "08:30")
    await feed(app, *UT, "08:31")
    assert spied(app) == []
    await feed(app, *UT, "08:32")
    assert spied(app) == [("UT", "report")]


async def test_source_change_reseeds(app):
    await feed(app, *FAR, "08:20")
    await switch(app, "located_via_ut_owntracks", "located_via_ut_telegram")
    await feed(app, *UT, "08:24")  # the first fix of the new source only seeds
    assert spied(app) == []
    await feed(app, *FAR, "08:26")
    assert spied(app) == [("UT", "leave")]


async def test_late_out_of_order_and_inaccurate_fixes_send_nothing(app):  # Review Focus 3
    await feed(app, *FAR, "08:20")
    await feed(app, *FAR, "08:30")
    await feed(app, *UT, "08:25", now="08:31")  # queued in a tunnel, delivered late
    await feed(app, *UT, "08:31", acc=400)  # worse than the 150 m station radius
    assert spied(app) == []
    await feed(app, *UT, "08:32", acc=20)
    assert spied(app) == [("UT", "enter")]


async def test_future_and_stale_fixes_change_nothing(app):
    await feed(app, *FAR, "08:20")
    await feed(app, *UT, "09:22", now="08:22")  # a phone clock an hour ahead
    await feed(app, *UT, "08:07", now="08:22")  # 15 min old
    assert spied(app) == []
    await feed(app, *UT, "08:23")
    assert spied(app) == [("UT", "enter")]


async def test_queued_fix_from_before_a_switch_back_is_dropped(app):
    await feed(app, *FAR, "08:20")
    held, release = asyncio.Event(), asyncio.Event()
    orig = app.rt.location

    async def hold(*args, **kw):
        held.set()
        await release.wait()
        await orig(*args, **kw)

    app.rt.location = hold
    app.clock.t = at("08:24")
    app.follower.feed("telegram", Fix(*UT, at("08:24")))
    await held.wait()
    app.follower.feed("telegram", Fix(*UT, at("08:24:30")))  # waits behind the lock
    await switch(app, "located_via_ut_owntracks", "located_via_ut_telegram")
    release.set()
    await app.follower.drain()
    app.rt.location = orig
    assert spied(app) == [("UT", "enter")]  # the held one; the core drops it on its epoch
    assert "UT" not in only_occurrence(app.rt).zone_reports
    await feed(app, *FAR, "08:26")  # seeds: the queued fix was neither sent nor a seed
    assert spied(app) == [("UT", "enter")]


async def test_owntracks_regions_give_home_leave(app_owntracks):
    app = app_owntracks
    await feed(app, *FAR, "07:45", regions=("home_area",))
    await feed(app, *FAR, "07:48")
    assert spied(app) == [("home_area", "leave")]
    lock = only_occurrence(app.rt).lock
    assert lock is not None and lock.commitment is Commitment.PROVISIONAL
    assert lock.source is LockSource.LOCATION


async def test_owntracks_role_name_is_not_a_region(app_owntracks):
    app = app_owntracks
    await feed(app, *FAR, "07:45", regions=("home",))
    await feed(app, *FAR, "07:48")
    assert spied(app) == []


async def test_only_profiles_with_that_source_get_the_fix(app):
    spied(app)
    app.clock.t = at("08:20")
    app.follower.feed("owntracks", Fix(*FAR, at("08:20")))
    app.clock.t = at("08:21")
    app.follower.feed("owntracks", Fix(*UT, at("08:21")))
    await app.follower.drain()
    assert spied(app) == []
    await feed(app, *UT, "08:22")  # the telegram profile's first fix: only a seed
    await feed(app, *FAR, "08:23")
    assert spied(app) == [("UT", "leave")]


async def test_no_positions_no_station_zone(tmp_path, tg):
    app = await located_app(tmp_path, tg, "located_via_ut_telegram", positions={})
    try:
        await feed(app, *FAR, "08:20")
        await feed(app, *UT, "08:24")
        await feed(app, *FAR, "08:30")
        assert spied(app) == []
    finally:
        await app.follower.close()


async def test_fixes_across_the_autumn_repeat_hour_keep_their_order(app):
    first = datetime(2026, 10, 25, 2, 50, tzinfo=AMS)  # 00:50 UTC
    second = datetime(2026, 10, 25, 2, 5, fold=1, tzinfo=AMS)  # 01:05 UTC
    assert second - first < timedelta(0)  # the wall clock says older
    await feed(app, *FAR, first)
    await feed(app, *UT, second)
    assert spied(app) == [("UT", "enter")]


def location_update(kind: str, hhmm: str, lat: object, lon: object, **extra: object) -> dict:
    body: dict = {"message_id": 1, "chat": {"id": CHAT}, "date": int(at("08:20").timestamp()),
                  "location": {"latitude": lat, "longitude": lon, **extra}}  # fmt: skip
    if kind == "edited_message":  # live location: each move edits the first message
        body["edit_date"] = int(at(hhmm).timestamp())
    return {"update_id": 1, kind: body}


async def test_telegram_location_messages_reach_the_follower(app):
    spied(app)
    steps = [
        ("08:20", location_update("message", "08:20", *FAR)),
        ("08:22", location_update("edited_message", "08:22", *UT, horizontal_accuracy=400)),
        ("08:23", location_update("edited_message", "08:23", "52.1", 5.1)),
        ("08:24", location_update("edited_message", "08:24", *UT, horizontal_accuracy=10)),
    ]
    for hhmm, update in steps:
        app.clock.t = at(hhmm)
        await app.bot.handle(update)
        await app.follower.drain()
    assert spied(app) == [("UT", "enter")]

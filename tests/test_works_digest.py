"""Weekly digests persist one current, recipient-pinned summary with explicit coverage gaps."""

import asyncio
from datetime import date, time, timedelta

import pytest
from runtime_rig import DAY, PROFILE, Rig, at

from train_commute_companion.model import EventKind, WorksDigestSettings
from train_commute_companion.profile import load_config
from train_commute_companion.providers.base import PlannedWorks, ProviderAnswer
from train_commute_companion.serde import dumps, loads
from train_commute_companion.store import PersistedState
from train_commute_companion.works_digest import check_week, due_week

TEXT = PROFILE[: PROFILE.index('  - id: "p2"')] + "    works_digest: {day: tue, time: '07:00'}\n"


def profile(text=TEXT):
    config, errors = load_config(text, None, None, DAY)
    assert config is not None and not errors, errors
    return config.profiles[0]


def digests(rig):
    return [event for event in rig.notifier.attempts if event.kind is EventKind.WORKS_DIGEST]


async def rig_at(tmp_path, when=None):
    rig = Rig(tmp_path)
    rig.clock.t = when or at("07:55")
    rig.write(text=TEXT)
    await rig.rt.start()
    await rig.rt.works_digest_idle()
    return rig


async def test_stopped_digest_owner_does_not_restart_queued_work(tmp_path):
    rig = Rig(tmp_path)
    rig.write(text=TEXT)
    await rig.rt.start()
    await rig.rt.stop_works_digest()
    rig.rt._refresh_digest_soon()
    await rig.rt.works_digest_idle()
    assert not rig.rt.state.works_digest_weeks
    assert not digests(rig)


async def test_failed_digest_can_retry_after_clock_rollback(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.write(text=TEXT)
    fetch = rig.rt._fetch_works
    first = True

    async def unavailable_once():
        nonlocal first
        if first:
            first = False
            raise RuntimeError("optional source unavailable")
        return await fetch()

    monkeypatch.setattr(rig.rt, "_fetch_works", unavailable_once)
    await rig.rt.start()
    await rig.rt.works_digest_idle()
    assert not rig.rt.state.works_digest_weeks
    rig.clock.jump(-1)
    await rig.rt.works_digest_once()
    assert rig.rt.state.works_digest_weeks["p1"] == date(2026, 10, 26)


@pytest.mark.parametrize(
    "setting",
    [
        "false",
        "{}",
        "{day: Tue, time: '07:00'}",
        "{day: tue}",
        "{day: tue, time: '24:00'}",
        "{day: tue, time: '07:00', enabled: true}",
    ],
)
def test_invalid_settings_refuse(setting):
    config, errors = load_config(
        TEXT.replace("{day: tue, time: '07:00'}", setting), None, None, DAY
    )
    assert config is None and errors


def test_default_off_and_stored_defaults():
    original = profile(PROFILE)
    assert original.works_digest is None
    assert due_week(original, at("07:55"), None) is None
    configured = profile()
    assert configured.works_digest == WorksDigestSettings(2, time(7))
    assert due_week(configured, at("06:59"), None) is None
    week = DAY - timedelta(days=DAY.weekday())
    assert due_week(configured, at("07:00"), None) == week
    assert due_week(configured, at("08:00"), week) is None
    assert due_week(configured, at("08:00"), week + timedelta(days=7)) is None
    assert due_week(configured, at("08:00", DAY + timedelta(days=1)), None) is None


async def test_real_runtime_once_per_week_restart_and_durable_audience(tmp_path):
    rig = await rig_at(tmp_path)
    assert len(digests(rig)) == 1
    text = digests(rig)[0].text
    assert "28 Oct to 03 Nov" in text
    assert "route coverage uncertain" in text  # The scripted world only contains today's trains.
    assert "not scheduled" in text
    assert "no planned-works indication" not in text
    assert rig.rt.state.works_digest_weeks["p1"] == date(2026, 10, 26)
    assert (
        loads(PersistedState, dumps(rig.rt.state)).works_digest_weeks
        == rig.rt.state.works_digest_weeks
    )
    await rig.rt.works_digest_once()
    await rig.restart()
    await rig.rt.works_digest_idle()
    assert len(digests(rig)) == 1


async def test_unknown_feed_never_means_all_clear_and_queries_cover_seven_days(tmp_path):
    rig = Rig(tmp_path)
    p = profile()
    requested = []

    async def works():
        return PlannedWorks((), "unknown")

    async def fetch(queries):
        requested.extend(queries)
        return tuple(ProviderAnswer(q, (), at("07:55")) for q in queries)

    rows = await check_week(rig.rt.state, p, at("07:55"), works, fetch, lambda: at("07:55"))
    assert len(rows) == 14
    assert len({r.commute_date for r in rows}) == 7
    assert {r.service_date for r in rows if r.status != "not scheduled"} == {
        date(2026, 10, d) for d in (28, 29, 30)
    } | {date(2026, 11, 2), date(2026, 11, 3)}
    assert len(requested) == 20  # Outbound has two legs + whole; return is direct.
    assert all("no planned-works" not in r.status for r in rows)


async def test_timeout_leaves_each_unchecked_date_explicitly_unknown(tmp_path, monkeypatch):
    import train_commute_companion.works_digest as module

    monkeypatch.setattr(module, "TIME_LIMIT_S", 0.01)

    async def works():
        await asyncio.Event().wait()

    async def fetch(queries):
        raise AssertionError("No route fetch after timeout")

    rows = await check_week(
        Rig(tmp_path).rt.state, profile(), at("07:55"), works, fetch, lambda: at("07:55")
    )
    assert sum(r.status == "unknown: check did not finish" for r in rows) == 10


async def test_failed_save_never_sends_private_digest_then_recovers(tmp_path):
    rig = await rig_at(tmp_path, at("06:55"))
    rig.store.fail = True
    rig.clock.t = at("07:05")
    await rig.rt.works_digest_once()
    assert not digests(rig)
    assert "p1" not in rig.rt.state.works_digest_weeks
    rig.store.fail = False
    rig.clock.jump(180)
    await rig.rt.works_digest_once()
    assert len(digests(rig)) == 1
    await rig.restart()
    await rig.rt.works_digest_idle()
    assert len(digests(rig)) == 1


async def test_verified_works_title_and_complete_route_checks(tmp_path):
    from dataclasses import replace

    from builders import make_connection, make_leg

    from train_commute_companion.model import Direction
    from train_commute_companion.providers.base import PlannedWork

    p = profile()
    p = replace(p, outbound=replace(p.outbound, transfers=()))
    now = at("07:55")
    hit_day = DAY + timedelta(days=1)

    async def works():
        return PlannedWorks(
            (
                PlannedWork(
                    at("00:00", hit_day),
                    at("23:59", hit_day),
                    frozenset({"AAA"}),
                    "Verified bridge work",
                ),
            ),
            "complete",
        )

    async def fetch(queries):
        results = []
        for q in queries:
            start = q.at + timedelta(minutes=15)
            end = start + timedelta(minutes=30)
            leg = make_leg(
                q.origin,
                q.destination,
                start.strftime("%H:%M"),
                end.strftime("%H:%M"),
                day=start.date(),
            )
            results.append(ProviderAnswer(q, (make_connection((leg,)),), now))
        return tuple(results)

    rows = await check_week(Rig(tmp_path).rt.state, p, now, works, fetch, lambda: now)
    positive = next(
        r for r in rows if r.commute_date == hit_day and r.direction is Direction.OUTBOUND
    )
    assert positive.status == "planned works may affect the checked route"
    assert positive.notes == ("NS planned works: Verified bridge work",)
    clean = next(
        r
        for r in rows
        if r.commute_date == DAY + timedelta(days=2) and r.direction is Direction.OUTBOUND
    )
    assert clean.status == "no planned-works indication found in these checks"


async def test_calendar_holidays_extra_dates_pauses_weekday_and_overnight(tmp_path):
    from dataclasses import replace
    from datetime import datetime

    from train_commute_companion.calendar import TZ
    from train_commute_companion.model import Direction

    p = profile()
    start = datetime(2026, 12, 24, 18, 0, tzinfo=TZ)
    p = replace(
        p,
        extra_dates=frozenset({date(2026, 12, 25)}),
        pauses=((date(2026, 12, 29), date(2026, 12, 29)),),
        outbound=replace(p.outbound, first_train_by_weekday={"mon": time(9, 12)}),
        return_=replace(p.return_, day_offset=1),
    )
    queries = []

    async def works():
        return PlannedWorks((), "complete")

    async def fetch(batch):
        queries.extend(batch)
        return tuple(ProviderAnswer(q, (), start) for q in batch)

    rows = await check_week(Rig(tmp_path).rt.state, p, start, works, fetch, lambda: start)
    assert (
        next(
            r
            for r in rows
            if r.commute_date == date(2026, 12, 25) and r.direction is Direction.OUTBOUND
        ).status
        != "not scheduled"
    )
    assert (
        next(
            r
            for r in rows
            if r.commute_date == date(2026, 12, 26) and r.direction is Direction.OUTBOUND
        ).status
        == "not scheduled"
    )
    assert (
        next(
            r
            for r in rows
            if r.commute_date == date(2026, 12, 28) and r.direction is Direction.RETURN
        ).status
        == "paused"
    )
    assert (
        next(
            r
            for r in rows
            if r.commute_date == date(2026, 12, 29) and r.direction is Direction.OUTBOUND
        ).status
        == "paused"
    )
    assert any(
        q.origin == "AAA"
        and q.at.date() == date(2026, 12, 28)
        and q.at.hour == 8
        and q.at.minute == 57
        for q in queries
    )


async def test_stale_or_mismatched_source_never_creates_clear_coverage(tmp_path):
    async def works():
        return PlannedWorks((), "complete")

    async def fetch(queries):
        return tuple(ProviderAnswer(q, (), at("07:54")) for q in queries)

    rows = await check_week(
        Rig(tmp_path).rt.state, profile(), at("07:55"), works, fetch, lambda: at("07:55")
    )
    assert all(
        r.status in {"not scheduled", "unknown: source evidence does not match this check"}
        for r in rows
    )


async def test_pending_digest_expires_and_route_edit_drops_it(tmp_path):
    from dataclasses import replace

    rig = await rig_at(tmp_path, at("06:55"))
    rig.notifier.fails = lambda event: event.kind is EventKind.WORKS_DIGEST
    rig.clock.t = at("07:05")
    await rig.rt.works_digest_once()
    pending = next(o for o in rig.rt.state.obligations if o.event.kind is EventKind.WORKS_DIGEST)
    assert pending.target_ids is not None
    rig.write(text=TEXT.replace('home_station: "AAA"', 'home_station: "BBB"'))
    await rig.rt.reload()
    assert not any(o.event.kind is EventKind.WORKS_DIGEST for o in rig.rt.state.obligations)
    # A saved audience never gets a previous day's summary after midnight.
    state = rig.rt.state
    rig.rt._writer.commit_strict(replace(state, obligations=(*state.obligations, pending)))
    before = len(digests(rig))
    rig.clock.t = at("00:01", DAY + timedelta(days=1))
    await rig.rt.watchdog_once()
    assert len(digests(rig)) == before
    assert not any(o.event.kind is EventKind.WORKS_DIGEST for o in rig.rt.state.obligations)


async def test_profile_edit_while_provider_awaits_discards_entire_digest(tmp_path):
    rig = await rig_at(tmp_path, at("06:55"))
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.provider.planned_works

    async def held():
        entered.set()
        await release.wait()
        return await original()

    rig.provider.planned_works = held
    rig.clock.t = at("07:05")
    task = asyncio.create_task(rig.rt.works_digest_once())
    await entered.wait()
    rig.write(text=TEXT.replace('home_station: "AAA"', 'home_station: "BBB"'))
    await rig.rt.reload()
    release.set()
    await task
    assert not digests(rig)
    assert not rig.rt.state.works_digest_weeks


@pytest.mark.parametrize("channels", [None, "[email]", "[primary, webhook]"])
async def test_digest_uses_existing_primary_email_webhook_category_routing(tmp_path, channels):
    from test_disruption_receipts import EMAIL, OTHER, World

    world = World(tmp_path)
    world.port.channels[OTHER] = "webhook"
    text = (
        TEXT
        if channels is None
        else TEXT + f"    alerts: {{channels: {{planned_works: {channels}}}}}\n"
    )
    world.rig.write(text=text)
    await world.start()
    await world.rig.rt.works_digest_idle()
    await world.rig.rt.webhooks_idle()
    phone = any(e.kind is EventKind.WORKS_DIGEST for e in world.rig.notifier.attempts)
    email = any(e.kind is EventKind.WORKS_DIGEST for e in world.records[EMAIL])
    webhook = any(e.kind is EventKind.WORKS_DIGEST for e in world.records[OTHER])
    assert (phone, email, webhook) == (
        (True, True, True)
        if channels is None
        else (False, True, False)
        if channels == "[email]"
        else (True, False, True)
    )


async def test_disabled_category_does_not_probe_or_deliver_digest(tmp_path):
    rig = Rig(tmp_path)
    rig.write(text=TEXT + "    alerts: {disabled_categories: [planned_works]}\n")
    await rig.rt.start()
    await rig.rt.works_digest_idle()
    assert not digests(rig)
    assert rig.provider.works_calls == 0
    assert not rig.rt.state.works_digest_weeks


async def test_optional_digest_task_does_not_delay_poll_and_cancels_cleanly(tmp_path):
    rig = await rig_at(tmp_path, at("06:55"))
    entered = asyncio.Event()

    async def held():
        entered.set()
        await asyncio.Event().wait()

    rig.provider.planned_works = held
    rig.clock.t = at("07:55")
    rig.rt._refresh_digest_soon()
    await asyncio.wait_for(entered.wait(), 1)
    before = len(rig.provider.calls)
    await asyncio.wait_for(rig.rt.poll_once(), 1)
    assert len(rig.provider.calls) > before
    await asyncio.wait_for(rig.rt.stop_works_digest(), 1)
    assert rig.rt._digest_task.cancelled()
    assert not rig.rt.state.works_digest_weeks and not digests(rig)


def test_dst_slot_resolution_and_backward_clock_week_guard():
    from dataclasses import replace
    from datetime import datetime

    from train_commute_companion.calendar import TZ

    p = replace(profile(), works_digest=WorksDigestSettings(7, time(2, 30)))
    before_gap = datetime(2026, 3, 29, 1, 59, tzinfo=TZ)
    after_gap = datetime(2026, 3, 29, 3, 0, tzinfo=TZ)
    assert due_week(p, before_gap, None) is None
    assert due_week(p, after_gap, None) == date(2026, 3, 23)
    first = datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=0)
    second = first.replace(fold=1)
    week = due_week(p, first, None)
    assert week == date(2026, 10, 19)
    assert due_week(p, second, week) is None
    assert due_week(p, after_gap, week) is None


async def test_same_route_old_pending_digest_never_replays_after_midnight_restart(tmp_path):
    rig = await rig_at(tmp_path, at("06:55"))
    rig.notifier.fails = lambda event: event.kind is EventKind.WORKS_DIGEST
    rig.clock.t = at("07:55")
    await rig.rt.works_digest_once()
    assert any(o.event.kind is EventKind.WORKS_DIGEST for o in rig.rt.state.obligations)
    before = len(digests(rig))
    rig.notifier.fails = lambda event: False
    rig.clock.t = at("00:01", DAY + timedelta(days=1))
    await rig.restart()
    await rig.rt.works_digest_idle()
    assert len(digests(rig)) == before
    assert not any(o.event.kind is EventKind.WORKS_DIGEST for o in rig.rt.state.obligations)


async def test_check_finishing_after_midnight_does_not_stage_old_digest(tmp_path):
    rig = await rig_at(tmp_path, at("06:55"))
    original = rig.provider.planned_works

    async def crossing():
        rig.clock.t = at("00:01", DAY + timedelta(days=1))
        return await original()

    rig.provider.planned_works = crossing
    rig.clock.t = at("23:59")
    await rig.rt.works_digest_once()
    assert not digests(rig) and not rig.rt.state.works_digest_weeks

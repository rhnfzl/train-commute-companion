import asyncio
from dataclasses import replace
from datetime import timedelta
from email.parser import BytesParser
from email.policy import default

import pytest
import runtime_seam as seam
from builders import ONE_TRANSFER, make_connection, make_leg, on_track
from lifecycle_fixtures import L1, L1X, L2
from runtime_rig import PROFILE, Rig, at, escape_world, late_usual_world
from tcc_app.email_settings import CopyPorts
from test_webhook_delivery import HOST, HOST2, URL, URL2, SyncRuntime, WRig

from train_commute_companion import email
from train_commute_companion.checked_alternatives import without_option
from train_commute_companion.location import LocationEvent
from train_commute_companion.model import EventKind, Provenance, alert_facts
from train_commute_companion.providers.base import MalformedAnswer, RateLimited
from train_commute_companion.webhook_queue import GIVE_UP

REPLACEMENT = make_connection(
    [
        make_leg("AAA", "UT", "08:25", "08:53", number="1300"),
        make_leg("UT", "RTD", "09:07", "09:44", number="7777"),
    ]
)
CANCELLED = make_connection([L1X, L2])


def warnings(rig):
    return [e.text for e in rig.notifier.attempts if e.kind is EventKind.CANCELLED]


def confirmation_calls(rig):
    return [q for q in rig.provider.calls if q.origin == "AAA" and q.at == at("08:25")]


@pytest.mark.parametrize("track", ["reported", "scheduled", None])
async def test_checks_replacement_once_and_reuses_it_for_retry(tmp_path, track):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.provider.world = [CANCELLED, REPLACEMENT]
    first = REPLACEMENT.legs[0]
    refreshed = replace(
        first,
        stops=(
            replace(
                first.board,
                planned_track="3" if track else None,
                actual_track="7" if track == "reported" else None,
            ),
            *first.stops[1:],
        ),
    )

    def reply(q):
        if q.origin == "AAA" and q.at == at("08:25"):
            rig.provider.world = [CANCELLED, make_connection([refreshed, REPLACEMENT.legs[1]])]

    rig.provider.hook = reply
    await rig.rt.poll_once()
    assert len(confirmation_calls(rig)) == 1
    assert "NS checked just now" in warnings(rig)[-1]
    if track == "reported":
        assert "platform 7" in warnings(rig)[-1]
    elif track == "scheduled":
        assert "scheduled platform 3" in warnings(rig)[-1]
    else:
        assert "platform" not in warnings(rig)[-1]
    rig.clock.jump(1)
    rig.notifier.fails = lambda e: False
    await rig.rt.watchdog_once()
    assert len(confirmation_calls(rig)) == 1
    assert "NS checked 1 min ago" in warnings(rig)[-1]


def test_legacy_next_option_is_removed_without_confirmation():
    from test_delivery import ob

    event = replace(
        ob(EventKind.CANCELLED).event,
        text="Office: your train is cancelled. Next option: 08:25, arrives 09:40. Choose it to follow it.",
    )
    assert without_option(event, None) == (
        "Office: your train is cancelled. Check the NS app for a current train option."
    )


async def test_dutch_confirmation_renders_the_checked_platform_and_leave_by(tmp_path):
    rig = Rig(tmp_path)
    rig.write(
        text=PROFILE.replace('    name: "NAME"', "    name: office_a\n    alerts: {language: nl}")
    )
    first, onward = REPLACEMENT.legs
    first = replace(first, stops=(replace(first.board, actual_track="7"), *first.stops[1:]))
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, make_connection([first, onward])]
    await rig.rt.poll_once()
    text = warnings(rig)[-1]
    assert "08:25" in text
    assert "Vertrek uiterlijk om 08:15" in text
    assert "NS-controle zojuist: rijdt, spoor 7." in text


async def test_a_new_board_with_a_running_train_invalidates_cached_cancellations(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.provider.world = [CANCELLED, REPLACEMENT]

    def reply(q):
        if q.origin == "AAA" and q.at == at("08:25"):
            rig.provider.world = [replace(REPLACEMENT, status="CANCELLED")]

    rig.provider.hook = reply
    await rig.rt.poll_once()
    assert "every train option" in warnings(rig)[-1]
    rig.clock.jump(1)
    rig.provider.hook = None
    rig.provider.world = [CANCELLED, REPLACEMENT]
    rig.notifier.fails = lambda e: False
    await rig.rt.poll_once()
    assert "every train option" not in warnings(rig)[-1]


@pytest.mark.parametrize("retry", [False, True])
async def test_other_connections_for_the_same_train_can_disagree_on_platform(tmp_path, retry):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    first, onward = REPLACEMENT.legs

    def tracked(actual):
        return replace(
            first,
            stops=(replace(first.board, actual_track=actual, planned_track="7"), first.alight),
        )

    later = make_leg("UT", "RTD", "09:12", "09:49", number="7979")
    checked = [CANCELLED, make_connection([tracked("7"), onward])]
    conflict = [*checked, make_connection([tracked("9"), later])]

    if retry:
        rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
        rig.provider.world = checked
        await rig.rt.poll_once()
        assert "runs, platform 7" in warnings(rig)[-1]
        rig.clock.jump(1)
        rig.provider.world = conflict
        await rig.rt.poll_once()
    else:
        rig.provider.world = conflict
        await rig.rt.poll_once()
        assert "NS checked just now: runs." in warnings(rig)[-1]
    assert "platform" not in warnings(rig)[-1]


@pytest.mark.parametrize(
    "outcome",
    [
        "cancelled",
        "missing",
        "wrong_day",
        "ambiguous",
        "conflicting_times",
        "incomplete",
        "uncatchable",
        "transfer",
        "rate",
    ],
)
async def test_warning_withholds_replacement_when_fresh_check_cannot_confirm(tmp_path, outcome):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    first, second = REPLACEMENT.legs
    dep = first.train.planned_departure
    if outcome == "rate":
        rig.provider.error = lambda q: RateLimited() if q.at == dep else None

    def reply(q):
        if q.origin != "AAA" or q.at != dep:
            return
        match outcome:
            case "cancelled":
                cancel = make_leg("AAA", "UT", "08:25", "08:53", number="1300", cancelled=True)
                rig.provider.world = [make_connection([cancel, second])]
            case "missing":
                rig.provider.world = []
            case "wrong_day":
                rig.provider.world = [
                    make_connection(
                        [
                            make_leg(
                                "AAA",
                                "UT",
                                "08:25",
                                "08:53",
                                number="1300",
                                day=dep.date() + timedelta(days=1),
                            ),
                            second,
                        ]
                    )
                ]
            case "ambiguous":
                rig.provider.world.append(
                    make_connection(
                        [
                            replace(first, train=replace(first.train, train_number="1301")),
                            second,
                        ]
                    )
                )
            case "conflicting_times":
                delayed = make_leg("AAA", "UT", "08:25", "08:53", number="1300", delay_dep=2)
                rig.provider.world.append(make_connection([delayed, second]))
            case "uncatchable":
                earlier = make_leg("AAA", "UT", "08:25", "08:53", number="1300", delay_dep=-24)
                rig.provider.world = [make_connection([earlier, second])]
            case "incomplete":
                rig.provider.world = [
                    make_connection(
                        [
                            make_leg(
                                "AAA",
                                "UT",
                                "08:25",
                                "08:53",
                                number="1300",
                                provenance=Provenance.PLANNED,
                            ),
                            second,
                        ]
                    )
                ]
            case "transfer":
                delayed = make_leg("AAA", "UT", "08:25", "08:53", number="1300", delay_arr=13)
                rig.provider.world = [make_connection([delayed, second])]

    rig.provider.hook = reply
    await rig.rt.poll_once()
    assert len(confirmation_calls(rig)) == 1
    message = warnings(rig)[-1]
    assert "08:25" not in message and "checked just now" not in message
    if outcome == "cancelled":
        assert "NS shows cancellations on every train option checked from AAA to RTD" in message
    else:
        assert "Check the NS app" in message
        assert "every train option" not in message


async def test_opt_out_preserves_existing_warning_and_request_count(tmp_path):
    rig = Rig(tmp_path)
    rig.write(
        text=PROFILE.replace(
            '    name: "NAME"', '    name: "NAME"\n    alerts: {checked_alternatives: false}'
        )
    )
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    await rig.rt.poll_once()
    assert not confirmation_calls(rig)
    assert "Next good connection: 08:25" in warnings(rig)[-1]
    assert "NS checked" not in warnings(rig)[-1]


async def test_all_cancelled_poll_names_only_observed_route_without_extra_request(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.provider.world = [
        CANCELLED,
        make_connection(
            [
                make_leg("AAA", "UT", "08:25", "08:53", number="1300", cancelled=True),
                REPLACEMENT.legs[1],
            ]
        ),
    ]
    await rig.rt.poll_once()
    assert not confirmation_calls(rig)
    message = warnings(rig)[-1]
    assert "NS shows cancellations on every train option checked from AAA to RTD" in message
    assert "strike" not in message and "Next good connection" not in message


async def test_reload_discards_confirmation_in_flight(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    entered, release, reloaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    apply_config = rig.rt._apply_config

    async def reply(q):
        if q.origin == "AAA" and q.at == at("08:25"):
            entered.set()
            await release.wait()

    def reload_applied(*args):
        apply_config(*args)
        reloaded.set()

    rig.provider.hook = reply
    monkeypatch.setattr(rig.rt, "_apply_config", reload_applied)
    poll = asyncio.create_task(rig.rt.poll_once())
    await asyncio.wait_for(entered.wait(), 2)
    rig.write("New name")
    reload = asyncio.create_task(rig.rt.reload())
    try:
        await asyncio.wait_for(reloaded.wait(), 2)
    finally:
        release.set()
    await asyncio.gather(poll, reload)
    assert len(confirmation_calls(rig)) == 1
    assert "08:25" not in warnings(rig)[-1] and "NS checked" not in warnings(rig)[-1]


@pytest.mark.parametrize("webhook", [False, True])
async def test_check_skips_an_alert_that_enters_quiet_hours_while_waiting(
    tmp_path, monkeypatch, webhook
):
    rig = WRig(tmp_path) if webhook else Rig(tmp_path)
    routing = ", channels: {advice: [webhook]}" if webhook else ""
    rig.write(
        text=PROFILE.replace(
            "    name:", f'    alerts: {{quiet_hours: ["08:15", "09:00"]{routing}}}\n    name:', 1
        )
    )
    rig.provider.world = list(ONE_TRANSFER)
    await rig.rt.start()
    await rig.run_until("08:06")
    await rig.rt.report_location("p1", LocationEvent("home", "still", at("08:06")))
    rig.clock.set("08:14")
    queued = asyncio.Event()
    prepare = rig.rt._checked.prepare

    async def wait_for_lock(*args):
        queued.set()
        return await prepare(*args)

    monkeypatch.setattr(rig.rt._checked, "prepare", wait_for_lock)
    async with rig.rt._checked._lock:
        poll = asyncio.create_task(rig.rt.poll_once())
        await asyncio.wait_for(queued.wait(), 2)
        rig.clock.set("08:15")
    await poll
    await rig.rt.webhooks_idle()
    assert not [q for q in rig.provider.calls if q.origin == "AAA" and q.at == at("08:44")]
    assert not [e for e in rig.notifier.attempts if e.kind is EventKind.NEXT_CONNECTION]
    if isinstance(rig, WRig):
        assert not rig.end.of(EventKind.NEXT_CONNECTION)


async def test_phone_smtp_and_webhook_share_confirmation(tmp_path, monkeypatch):
    rig = WRig(tmp_path)
    letters = []

    async def transport(settings, body):
        letters.append(BytesParser(policy=default).parsebytes(body).get_content())

    monkeypatch.setattr(email, "send", transport)
    smtp = email.EmailPoster(
        email.EmailSettings("localhost", 465, "ssl", "sender@example.com", "recipient@example.com"),
        rig.clock.now,
    )
    rig.rt = SyncRuntime(
        profile_path=rig.path,
        store=rig.store,
        provider=rig.provider,
        notifier=rig.notifier,
        clock=rig.clock,
        require_stations=False,
        webhooks=CopyPorts(rig.poster, smtp),
    )
    await rig.rt.start()
    letters.clear()
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    await rig.rt.poll_once()
    assert len(confirmation_calls(rig)) == 1
    message = warnings(rig)[-1]
    assert "NS checked just now" in message
    assert rig.end.of(EventKind.CANCELLED)[0]["text"] == message
    assert any(message in letter.splitlines() for letter in letters)


@pytest.mark.parametrize("primary", [True, False])
async def test_earlier_train_can_reach_webhook_before_first_successful_primary(tmp_path, primary):
    text = (
        PROFILE
        if primary
        else PROFILE.replace(
            '    name: "NAME"', '    name: "NAME"\n    alerts: {channels: {advice: [webhook]}}'
        )
    )
    rig = WRig(tmp_path, "16:54", text=text)
    rig.provider.window = timedelta(hours=1)
    rig.provider.world = late_usual_world()
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    await rig.rt.start()
    checks = [q for q in rig.provider.calls if q.at == at("17:22")]
    assert len(checks) == 1
    posted = rig.end.of(EventKind.LEAVE_NOW)
    assert len(posted) == 1 and "NS checked just now" in posted[0]["text"]
    if primary:
        attempted = next(e for e in rig.notifier.attempts if e.kind is EventKind.LEAVE_NOW)
        assert attempted.text == posted[0]["text"]
    await rig.rt.watchdog_once()
    assert len(rig.end.of(EventKind.LEAVE_NOW)) == 1
    assert len([q for q in rig.provider.calls if q.at == at("17:22")]) == 1


async def test_advice_withdrawal_reaches_only_the_destination_that_got_the_advice(tmp_path):
    rig = WRig(tmp_path, "16:54")
    rig.provider.window = timedelta(hours=1)
    rig.provider.world = late_usual_world()
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    await rig.rt.start()
    assert len(rig.end.of(EventKind.LEAVE_NOW)) == 1
    rig.clock.set("16:56")
    rig.provider.world = [
        make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952")]),
        *escape_world(),
    ]
    await rig.rt.poll_once()
    assert len(rig.end.of(EventKind.EARLIER_TRAIN_WITHDRAWN)) == 1
    assert not any(e.kind is EventKind.EARLIER_TRAIN_WITHDRAWN for e in rig.notifier.attempts)


@pytest.mark.parametrize("retained_route", [False, True])
async def test_earlier_train_retry_keeps_confirmation_across_language_edit(
    tmp_path, retained_route
):
    rig = Rig(tmp_path, "16:52" if retained_route else "16:54")
    rig.provider.window = timedelta(hours=1)
    rig.provider.world = late_usual_world()
    rig.notifier.fails = lambda e: e.kind is EventKind.LEAVE_NOW
    if retained_route:
        rig.provider.world = [
            *escape_world(),
            make_connection([make_leg("RTD", "AAA", "17:52", "18:30", number="7952")]),
        ]
    await rig.rt.start()
    if retained_route:
        rig.write(
            text=rig.path.read_text().replace('home_station: "AAA"', 'home_station: "BBB"', 1)
        )
        assert await rig.rt.reload() is None
        rig.clock.set("16:54")
        rig.provider.world = late_usual_world()
        await rig.rt.poll_once()
    assert len([e for e in rig.notifier.attempts if e.kind is EventKind.LEAVE_NOW]) == 1
    rig.write(
        text=rig.path.read_text().replace(
            '    name: "office_a"',
            '    name: "office_a"\n    alerts: {language: nl}',
            1,
        )
    )
    rig.notifier.fails = lambda e: False
    assert await rig.rt.reload() is None
    await rig.rt.watchdog_once()
    first, retry = [e for e in rig.notifier.attempts if e.kind is EventKind.LEAVE_NOW]
    assert first.key == retry.key
    assert first.text == retry.text and "NS checked just now" in retry.text
    assert len([q for q in rig.provider.calls if q.at == at("17:22")]) == 1


async def test_disabled_warning_keeps_a_checked_routine_replacement_message(tmp_path):
    rig = Rig(tmp_path, "07:55")
    rig.write(
        text=PROFILE.replace(
            '    name: "NAME"', '    name: "NAME"\n    alerts: {disabled_categories: [journey]}'
        )
    )
    first, onward = REPLACEMENT.legs

    def tracked(track):
        return replace(first, stops=(replace(first.board, actual_track=track), *first.stops[1:]))

    rig.provider.world = [CANCELLED, make_connection([tracked("3"), onward])]

    def reply(q):
        if q.origin == "AAA" and q.at == at("08:25"):
            rig.provider.world = [CANCELLED, make_connection([tracked("7"), onward])]

    rig.provider.hook = reply
    await rig.rt.start()
    (check_in,) = [e.text for e in rig.notifier.attempts if e.kind is EventKind.ALL_GOOD]
    assert "NS checked just now: runs, platform 7." in check_in
    assert "platform 3" not in check_in
    assert not warnings(rig)


async def test_routine_fallback_omits_maps_for_an_unconfirmed_option(tmp_path):
    rig = Rig(tmp_path, "07:52")
    rig.write(
        text=PROFILE.replace(
            "    name:",
            "    alerts: {disabled_categories: [journey]}\n    name:",
            1,
        )
    )
    rig.rt._primary_destination = lambda: "ha-mobile:phone"
    rig.provider.positions["AAA"] = (52.1, 5.2)
    rig.provider.world = [CANCELLED, REPLACEMENT]
    rig.provider.error = lambda q: MalformedAnswer() if q.at == at("08:25") else None
    await rig.rt.start()
    (check_in,) = [e for e in rig.notifier.attempts if e.kind is EventKind.ALL_GOOD]
    assert "Check the NS app" in check_in.text
    assert alert_facts(check_in).navigation is None


async def test_auxiliary_retry_reuses_confirmation_and_preserves_receipts(tmp_path):
    rig = WRig(tmp_path, urls=(URL, URL2))
    await rig.rt.start()
    primary_copy, retry_copy = rig.ends[HOST], rig.ends[HOST2]
    retry_copy.status = 503
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    await rig.rt.poll_once()
    assert len(confirmation_calls(rig)) == 1
    (copy_id,) = retry_copy.ids(EventKind.CANCELLED)
    assert primary_copy.ids(EventKind.CANCELLED) == [copy_id]
    assert retry_copy.of(EventKind.CANCELLED) == []

    rig.clock.jump(1)
    retry_copy.status = 200
    await rig.rt.watchdog_once()
    assert primary_copy.ids(EventKind.CANCELLED) == [copy_id]
    assert retry_copy.ids(EventKind.CANCELLED) == [copy_id, copy_id]
    assert "NS checked 1 min ago" in retry_copy.of(EventKind.CANCELLED)[0]["text"]
    assert len(confirmation_calls(rig)) == 1


async def test_webhook_copy_expiring_while_waiting_skips_confirmation(tmp_path, monkeypatch):
    rig = WRig(tmp_path)
    await rig.rt.start()
    rig.end.status = 503
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    await rig.rt.poll_once()
    queue = rig.rt._queue
    (copy,) = [c for c in queue.copies.values() if c.obligation.event.kind is EventKind.CANCELLED]
    rig.clock.jump(1)
    queue.copies[copy.id] = replace(
        copy, first_try=rig.clock.now() - GIVE_UP + timedelta(seconds=1)
    )
    rig.end.status = 200
    rig.rt._checked._samples.clear()
    calls = len(confirmation_calls(rig))
    queued = asyncio.Event()
    check = rig.rt._checked.prepare

    async def wait_for_lock(*args):
        queued.set()
        return await check(*args)

    monkeypatch.setattr(rig.rt._checked, "prepare", wait_for_lock)
    async with rig.rt._checked._lock:
        queue.post_soon()
        await asyncio.wait_for(queued.wait(), 2)
        rig.clock.jump(0.05)
    await queue.idle()

    assert len(confirmation_calls(rig)) == calls
    assert not rig.end.of(EventKind.CANCELLED)
    assert copy.id not in queue.copies


async def test_cancellation_summary_requires_only_cancelled_train_options(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.provider.world = [CANCELLED, REPLACEMENT]
    unknown = make_leg("AAA", "RTD", "08:36", "09:30", number="1500", provenance=Provenance.PLANNED)

    def reply(q):
        if q.origin == "AAA" and q.at == at("08:25"):
            cancelled = make_leg("AAA", "UT", "08:25", "08:53", number="1300", cancelled=True)
            rig.provider.world = [
                make_connection([cancelled, REPLACEMENT.legs[1]]),
                make_connection([unknown]),
            ]

    rig.provider.hook = reply
    await rig.rt.poll_once()
    assert "every train option" not in warnings(rig)[-1]
    assert all(hhmm not in warnings(rig)[-1] for hhmm in ("08:25", "08:36"))


async def test_new_board_invalidates_platform_until_another_budgeted_check(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.provider.world = [CANCELLED, REPLACEMENT]
    await rig.rt.poll_once()
    assert "NS checked just now" in warnings(rig)[-1]

    first, second = REPLACEMENT.legs
    changed = replace(first, stops=(replace(first.board, actual_track="9"), *first.stops[1:]))
    rig.provider.world = [CANCELLED, make_connection([changed, second])]
    rig.clock.jump(1)
    await rig.rt.poll_once()
    normal_poll_count = len(confirmation_calls(rig))
    assert normal_poll_count == 2  # the regular poll also queries the train we now follow
    await rig.rt.watchdog_once()
    assert len(confirmation_calls(rig)) == normal_poll_count
    assert "08:25" not in warnings(rig)[-1] and "NS checked" not in warnings(rig)[-1]

    rig.clock.jump(1)
    await rig.rt.watchdog_once()
    assert len(confirmation_calls(rig)) == normal_poll_count + 1
    assert "NS checked just now: runs, platform 9." in warnings(rig)[-1]


@pytest.mark.parametrize("cancelled_first", [False, True])
async def test_newer_itinerary_conflicting_status_blocks_a_cached_confirmation(
    tmp_path, cancelled_first
):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    first, second = REPLACEMENT.legs
    running = make_connection([on_track(first, "7"), second])
    rig.provider.world = [CANCELLED, running]
    await rig.rt.poll_once()
    assert "runs, platform 7" in warnings(rig)[-1]
    cancelled = replace(running, status="CANCELLED")
    rig.provider.world = (
        [CANCELLED, cancelled, running] if cancelled_first else [CANCELLED, running, cancelled]
    )
    rig.clock.jump(1)
    await rig.rt.poll_once()
    assert "runs, platform 7" not in warnings(rig)[-1]


async def test_no_usual_train_still_reports_scoped_cancelled_options(tmp_path):
    rig = Rig(tmp_path)
    rig.provider.world = [
        make_connection(
            [
                make_leg("AAA", "RTD", "08:25", "09:30", number="1300", cancelled=True),
            ]
        )
    ]
    await rig.rt.start()
    assert not confirmation_calls(rig)
    no_connection = next(e for e in rig.rt.events_log if e.kind is EventKind.NO_CONNECTION)
    assert (
        "NS shows cancellations on every train option checked from AAA to RTD" in no_connection.text
    )


async def test_startup_does_not_check_in_for_the_cancelled_replacement(tmp_path):
    rig = Rig(tmp_path, "07:55")
    rig.provider.world = [CANCELLED, REPLACEMENT]

    def reply(q):
        if q.origin == "AAA" and q.at == at("08:25"):
            cancelled = make_leg("AAA", "UT", "08:25", "08:53", number="1300", cancelled=True)
            rig.provider.world = [make_connection([cancelled, REPLACEMENT.legs[1]])]

    rig.provider.hook = reply
    await rig.rt.start()
    assert len(confirmation_calls(rig)) == 1
    assert "every train option" in warnings(rig)[-1]
    assert all("08:25" not in e.text for e in rig.rt.events_log)


@pytest.mark.parametrize("legacy", [False, True])
async def test_relock_does_not_reuse_confirmation_for_a_different_train(tmp_path, legacy):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    later = make_connection(
        [
            make_leg("AAA", "UT", "08:40", "09:06", number="1400"),
            make_leg("UT", "RTD", "09:21", "10:00", number="8888"),
        ]
    )
    rig.provider.world = [CANCELLED, REPLACEMENT, later]
    await rig.rt.poll_once()
    assert "08:25" in warnings(rig)[-1] and "NS checked just now" in warnings(rig)[-1]

    if legacy:
        obligations = tuple(
            replace(
                o,
                event=replace(
                    o.event,
                    text="Office: your train is cancelled. Next option: 08:19, arrives 09:40. "
                    "Choose it to follow it.",
                    facts=None,
                ),
            )
            if o.event.kind is EventKind.CANCELLED
            else o
            for o in rig.rt.state.obligations
        )
        seam.seed_state(rig.rt, obligations=obligations)
        seam.save_state(rig.rt)
    rig.clock.jump(1)
    cancelled = make_leg("AAA", "UT", "08:25", "08:53", number="1300", cancelled=True)
    rig.provider.world = [CANCELLED, make_connection([cancelled, REPLACEMENT.legs[1]]), later]
    await rig.rt.poll_once()
    assert not any(q.at == at("08:40") for q in rig.provider.calls)
    assert all("08:40" not in text for text in warnings(rig)[-2:])
    assert all("NS checked" not in text for text in warnings(rig)[-2:])
    assert all("08:19" not in text for text in warnings(rig)[-2:])


async def test_checked_reroute_names_new_transfer_stations(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    first = replace(L1, stops=(*L1.stops[:-1], replace(L1.alight, name="Utrecht Centraal")))
    onward = make_leg("UT", "BBB", "08:52", "09:00", number="3100")
    onward = replace(onward, stops=(onward.board, replace(onward.alight, name="New Station")))
    rig.provider.world = [
        make_connection(
            [
                first,
                onward,
                make_leg("BBB", "RTD", "09:10", "09:40", number="3200"),
            ]
        )
    ]
    await rig.rt.poll_once()
    (message,) = [e.text for e in rig.notifier.attempts if e.kind is EventKind.ROUTE_CHANGED]
    assert "via Utrecht Centraal, New Station, following it" in message
    assert "NS checked just now: runs" in message


async def test_failed_new_poll_does_not_reuse_prior_confirmation(tmp_path):
    rig = Rig(tmp_path)
    await rig.rt.start()
    rig.clock.set("07:52")
    rig.notifier.fails = lambda e: e.kind is EventKind.CANCELLED
    rig.provider.world = [CANCELLED, REPLACEMENT]
    await rig.rt.poll_once()
    assert "NS checked just now" in warnings(rig)[-1]

    rig.clock.jump(0.5)
    rig.provider.error = lambda q: MalformedAnswer("incomplete")
    rig.notifier.fails = lambda e: False
    await rig.rt.poll_once()
    assert "08:25" not in warnings(rig)[-1] and "NS checked" not in warnings(rig)[-1]

import asyncio
import base64
import json
import threading
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
import runtime_seam as seam
from app_rig import CODES, ONE_PROFILE, STRANGER, TickClock, free_port, make_app, until
from lookahead_fixtures import DAYS, NOW, OUT, YAML, confirmed, probe
from runtime_rig import PROFILE, USUAL, Provider, StationsProvider, at
from tcc_app.bot import ShellState, save_state
from tcc_app.main import Settings, build, dispatch_mcp, serve
from tcc_app.ntfy import NtfyNotifier, NtfyProof, NtfySettings, ntfy_settings
from tcc_app.web import health_report, start_web
from test_web import ask

from train_commute_companion.delivery import deliver
from train_commute_companion.lookahead import render_message
from train_commute_companion.lookahead_probe import bundle_key
from train_commute_companion.lookahead_run import ProbedRun
from train_commute_companion.model import (
    AlertEvent,
    AlertSettings,
    Direction,
    EventKind,
    Obligation,
    Severity,
    direction_key,
)
from train_commute_companion.providers.base import ActiveDisruption, ActiveDisruptions
from train_commute_companion.state import BaselineHistory, LookAheadFact, LookAheadLedger
from train_commute_companion.store import StorageError

TOPIC = "random-test-topic-0123456789abcdef0123456789"
TOKEN = "tk_private-test-token-0123456789"
HEALTH = "test-local-health-token-0123456"
ENV = {"NS_API_KEY": "test-ns-key", "TCC_NTFY_TOPIC": TOPIC, "TCC_NTFY_TOKEN": TOKEN}


def alert():
    return AlertEvent(
        "p1",
        "Office",
        None,
        None,
        None,
        EventKind.CANCELLED,
        Severity.LOUD,
        None,
        "Train cancelled",
        "cancelled",
        at("08:00"),
    )


def test_ntfy_only_settings_defaults_disabled_and_telegram_compatibility():
    settings = Settings.from_env(ENV)
    assert settings.bot_token == "" and settings.chat_id is None
    assert settings.ntfy == NtfySettings(TOPIC, TOKEN)
    assert ntfy_settings({}) is None
    assert ntfy_settings({**ENV, "TCC_NTFY_ENABLED": "false"}) is None
    assert Settings.from_env({"NS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "123:test"}).ntfy is None
    with pytest.raises(ValueError, match="Telegram or ntfy"):
        Settings.from_env({**ENV, "TCC_NTFY_ENABLED": "false"})
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID requires"):
        Settings.from_env({**ENV, "TELEGRAM_CHAT_ID": "123"})
    assert TOPIC not in repr(settings) and TOKEN not in repr(settings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("TCC_NTFY_TOPIC", ""),
        ("TCC_NTFY_TOPIC", "guessable"),
        ("TCC_NTFY_TOPIC", TOPIC + "/secret"),
        ("TCC_NTFY_TOKEN", ""),
        ("TCC_NTFY_TOKEN", "short"),
        ("TCC_NTFY_TOKEN", "token\r\nX-Header: secret"),
        ("TCC_NTFY_SERVER", "http://private.invalid"),
        ("TCC_NTFY_SERVER", "https://private.invalid/topic"),
        ("TCC_NTFY_SERVER", "https://secret@private.invalid"),
        ("TCC_NTFY_SERVER", "https://private.invalid/?token=secret"),
        ("TCC_NTFY_SERVER", "https://private.invalid/#secret"),
        ("TCC_NTFY_SERVER", "https://private.invalid:0"),
        ("TCC_NTFY_ENABLED", "yes"),
    ],
)
def test_settings_refuse_partial_or_unsafe_config_without_echoing_secrets(field, value):
    with pytest.raises(ValueError) as error:
        Settings.from_env({**ENV, field: value})
    assert field in str(error.value)
    assert TOKEN not in str(error.value) and TOPIC not in str(error.value)
    if value:
        assert value not in str(error.value)


@pytest.mark.parametrize(
    "partial",
    [
        {"TCC_NTFY_TOKEN": TOKEN},
        {"TCC_NTFY_TOPIC": TOPIC},
        {"TCC_NTFY_SERVER": "https://private.invalid"},
    ],
)
def test_partial_settings_fail_even_with_telegram(partial):
    with pytest.raises(ValueError, match="TCC_NTFY"):
        Settings.from_env({"NS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "123:test", **partial})


def test_destination_binds_canonical_server_topic_and_authentication():
    settings = NtfySettings(TOPIC, TOKEN)
    assert (
        settings.destination_id == replace(settings, server="https://NTFY.SH:443/").destination_id
    )
    for changed in (
        replace(settings, topic=TOPIC + "x"),
        replace(settings, token=TOKEN + "x"),
        replace(settings, server="https://own.invalid"),
    ):
        assert settings.destination_id != changed.destination_id
        assert TOKEN not in changed.destination_id and TOPIC not in changed.destination_id


async def test_priorities_only_send_text_no_callbacks_and_never_follow_redirects(caplog):
    requests = []
    status = 200

    def receive(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"location": "https://other.invalid/stolen"}, text=TOPIC + TOKEN
        )

    clock = TickClock(at("07:50"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), follow_redirects=True
    ) as client:
        notifier = NtfyNotifier(NtfySettings(TOPIC, TOKEN), client, clock.now)
        event = alert()
        assert await notifier.send(event)
        assert await notifier.send(replace(event, severity=Severity.SILENT))
        bodies = [json.loads(r.content) for r in requests]
        assert [b["priority"] for b in bodies] == [4, 1]
        assert all(set(b) == {"topic", "message", "priority"} for b in bodies)
        assert all(b["message"] == event.text and b["topic"] == TOPIC for b in bodies)
        assert all(str(r.url) == "https://ntfy.sh/" for r in requests)
        assert all(r.headers["Authorization"] == "Bearer " + TOKEN for r in requests)
        status = 307
        assert not await notifier.send(event)
        assert len(requests) == 3
        notifier.retire()
        clock.jump(2)
        assert not await notifier.send(event) and len(requests) == 3
    assert (
        TOPIC not in caplog.text and TOKEN not in caplog.text and "other.invalid" not in caplog.text
    )


@pytest.mark.parametrize("failure", [401, 403, 429, 500, "timeout"])
async def test_delivery_failure_retries_existing_obligation_after_backoff(
    failure, caplog, tmp_path, tg
):
    requests = []
    failing = True

    def receive(request):
        requests.append(request)
        if failing:
            if failure == "timeout":
                raise httpx.ReadTimeout(TOKEN + TOPIC, request=request)
            return httpx.Response(failure, text=TOKEN + TOPIC)
        return httpx.Response(200)

    clock = TickClock(at("07:50"))
    tg.other = receive
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    seam.app_warning(app.rt, EventKind.KEY_REJECTED, "test queued alert", clock.now())
    await seam.deliver(app.rt)
    pending = next(o for o in app.rt.state.obligations if "test queued alert" in o.event.text)
    assert pending.attempts == 1
    count = len(requests)
    await seam.deliver(app.rt)
    assert len(requests) == count
    failing = False
    clock.jump(1)
    await seam.deliver(app.rt)
    assert not app.rt.state.obligations
    assert sum("test queued alert" in json.loads(r.content)["message"] for r in requests) == 1
    assert not tg.calls
    assert TOPIC not in caplog.text and TOKEN not in caplog.text


async def test_queued_alert_does_not_move_to_changed_ntfy_destination(tmp_path, tg):
    bodies = []
    tg.other = lambda r: bodies.append(json.loads(r.content)) or httpx.Response(500)
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    seam.app_warning(app.rt, EventKind.KEY_REJECTED, "old private alert", clock.now())
    await seam.deliver(app.rt)
    assert app.rt.state.obligations
    tg.other = lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200)
    changed = make_app(
        tmp_path, tg, clock, ntfy=NtfySettings(TOPIC + "x", TOKEN), bot_token="", chat_id=None
    )
    count = len(bodies)
    await changed.rt.start()
    await seam.deliver(changed.rt)
    assert not any("old private alert" in b["message"] for b in bodies[count:])


@pytest.mark.parametrize(
    "settings,kind,severity,verdict",
    [
        (
            AlertSettings(channels={"journey": frozenset({"primary"})}),
            EventKind.CANCELLED,
            Severity.LOUD,
            "sent",
        ),
        (
            AlertSettings(ntfy_enabled=False),
            EventKind.CANCELLED,
            Severity.LOUD,
            "dropped",
        ),
        (
            AlertSettings(disabled_categories=frozenset({"journey"})),
            EventKind.CANCELLED,
            Severity.LOUD,
            "dropped",
        ),
        (
            AlertSettings(quiet_hours=(at("07:00").time(), at("09:00").time())),
            EventKind.TIMETABLE_ADOPTED,
            Severity.SILENT,
            "held",
        ),
    ],
)
async def test_existing_profile_controls_apply_to_ntfy_primary(
    tmp_path, tg, settings, kind, severity, verdict
):
    bodies = []
    tg.other = lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200)
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    config = app.rt.state.config
    profile = replace(config.profiles[0], alerts=settings)
    seam.seed_state(app.rt, config=replace(config, profiles=(profile,)))
    event = replace(alert(), kind=kind, severity=severity)
    obligation = Obligation(
        "test-controls", event, None, None, config.revision, None, "fact", None, 0
    )
    bodies.clear()
    remaining, sent = await deliver(
        obligation, seam.ledger(app.rt), app.notifier, clock.now(), app.notifier.destination_id
    )
    assert sent == (verdict == "sent")
    assert (remaining == obligation) == (verdict == "held")
    assert len(bodies) == (1 if sent else 0)


async def test_disabling_ntfy_stops_pending_primary_but_keeps_pinned_auxiliary_copy(tmp_path, tg):
    requests = []
    tg.other = lambda request: requests.append(request.url.host) or httpx.Response(500)
    clock = TickClock(at("07:50"))
    app = make_app(
        tmp_path,
        tg,
        clock,
        ntfy=NtfySettings(TOPIC, TOKEN),
        bot_token="",
        chat_id=None,
        webhook_urls=("https://copy.example.test/hook",),
        webhook_secret="test-secret",
    )
    await app.rt.start()
    config = app.rt.state.config
    event = replace(alert(), kind=EventKind.KEY_REJECTED)
    obligation = seam.obligation(app.rt, event, None, clock.now())
    seam.seed_state(app.rt, obligations=(obligation,))
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert app.rt.state.obligations
    pinned = app.rt.state.obligations[0].target_ids
    assert app.notifier.destination_id in pinned

    profile = replace(config.profiles[0], alerts=AlertSettings(ntfy_enabled=False))
    seam.seed_state(app.rt, config=replace(config, profiles=(profile,)))
    requests.clear()
    clock.t = at("07:52")
    tg.other = lambda request: (
        requests.append((request.url.host, json.loads(request.content)["text"]))
        or httpx.Response(200)
    )
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert requests.count(("copy.example.test", event.text)) == 1
    assert {host for host, _ in requests} == {"copy.example.test"}
    assert not app.rt.state.obligations
    assert not seam.owed_copies(app.rt)


async def test_ntfy_disabled_at_creation_does_not_gain_a_primary_recipient_after_enabling(
    tmp_path, tg
):
    requests = []
    tg.other = lambda request: requests.append(request.url.host) or httpx.Response(200)
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    config = app.rt.state.config
    profile = replace(
        config.profiles[0],
        alerts=AlertSettings(
            ntfy_enabled=False, quiet_hours=(at("07:00").time(), at("09:00").time())
        ),
    )
    seam.seed_state(app.rt, config=replace(config, profiles=(profile,)))
    event = replace(alert(), kind=EventKind.TIMETABLE_ADOPTED, severity=Severity.SILENT)
    obligation = seam.obligation(app.rt, event, None, clock.now())
    seam.seed_state(app.rt, obligations=(obligation,))
    assert app.rt.state.obligations[0].primary_suppressed
    seam.seed_state(app.rt, config=config)
    requests.clear()
    await seam.deliver(app.rt)
    assert not requests


async def test_ntfy_off_on_during_quiet_hours_does_not_revive_a_saved_alert(tmp_path, tg):
    requests = []
    tg.other = lambda request: requests.append(request.url.host) or httpx.Response(200)
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    config = app.rt.state.config
    profile = replace(
        config.profiles[0],
        alerts=AlertSettings(quiet_hours=(at("07:00").time(), at("09:00").time())),
    )
    config = replace(config, profiles=(profile,))
    event = replace(alert(), kind=EventKind.TIMETABLE_ADOPTED, severity=Severity.SILENT)
    seam.seed_state(
        app.rt, config=config, obligations=(seam.obligation(app.rt, event, None, clock.now()),)
    )
    await seam.deliver(app.rt)
    assert app.rt.state.obligations
    disabled = replace(profile, alerts=replace(profile.alerts, ntfy_enabled=False))
    seam.seed_state(app.rt, config=replace(config, profiles=(disabled,)))
    seam.save_state(app.rt)
    seam.seed_state(app.rt, config=config)
    seam.save_state(app.rt)
    clock.t = at("09:01")
    requests.clear()
    await seam.deliver(app.rt)
    assert not requests
    assert not app.rt.state.obligations


async def test_ntfy_created_off_while_a_send_waits_cannot_send_after_reenable(
    tmp_path, tg, monkeypatch
):
    requests = []
    tg.other = lambda request: (
        requests.append(json.loads(request.content)["message"]) or httpx.Response(200)
    )
    clock = TickClock(at("07:20"))
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    config = app.rt.state.config
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.notifier.send

    async def blocked(event):
        if event.key == "block":
            entered.set()
            await release.wait()
            return True
        return await original(event)

    monkeypatch.setattr(app.notifier, "send", blocked)
    first = seam.obligation(
        app.rt, replace(alert(), key="block", kind=EventKind.KEY_REJECTED), None, clock.now()
    )
    seam.seed_state(app.rt, obligations=(first,))
    sending = asyncio.create_task(seam.deliver(app.rt))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        disabled = replace(config.profiles[0], alerts=AlertSettings(ntfy_enabled=False))
        later = seam.obligation(
            app.rt,
            replace(
                alert(), key="later", text="Created while ntfy was off", kind=EventKind.KEY_REJECTED
            ),
            None,
            clock.now(),
        )
        seam.seed_state(
            app.rt,
            config=replace(config, profiles=(disabled,)),
            obligations=(*app.rt.state.obligations, later),
        )
        seam.save_state(app.rt)
        seam.seed_state(app.rt, config=config)
        seam.save_state(app.rt)
        release.set()
        await asyncio.wait_for(sending, 2)
        await seam.deliver(app.rt)
        assert not any("Created while ntfy was off" in body for body in requests)
    finally:
        release.set()
        await sending


@pytest.mark.parametrize("lane", ["main", "lookahead", "digest"])
async def test_ntfy_optout_persists_in_every_outbox_lane(tmp_path, tg, lane):
    tg.other = lambda request: httpx.Response(200)
    clock = TickClock(at("07:20"))
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    config = app.rt.state.config
    disabled = replace(config.profiles[0], alerts=AlertSettings(ntfy_enabled=False))
    event = replace(
        alert(), kind=EventKind.WORKS_DIGEST if lane == "digest" else EventKind.WORKS_AHEAD
    )
    obligation = seam.obligation(app.rt, event, None, clock.now())
    if lane == "lookahead":
        obligation = replace(obligation, look_ahead=("p1", Direction.OUTBOUND, clock.now().date()))
        changes = {
            "lookahead": {
                "p1|outbound": LookAheadLedger({}, (obligation,), None, config.revision, None)
            }
        }
        pending = lambda state: state.lookahead["p1|outbound"].pending[0]
    else:
        changes = {
            "obligations": (
                replace(obligation, digest_token="test-digest") if lane == "digest" else obligation,
            )
        }
        pending = lambda state: state.obligations[0]
    seam.seed_state(app.rt, config=replace(config, profiles=(disabled,)), **changes)
    seam.save_state(app.rt)
    assert pending(seam.store(app.rt).load()).primary_suppressed
    # A later write from before the opt-out must not restore the old permission.
    seam.seed_state(app.rt, config=config, **changes)
    seam.save_state(app.rt)
    assert pending(seam.store(app.rt).load()).primary_suppressed


async def test_ntfy_profile_switch_does_not_disable_telegram_primary(tmp_path, tg):
    clock = TickClock(at("07:50"))
    app = make_app(tmp_path, tg, clock)
    await app.rt.start()
    config = app.rt.state.config
    profile = replace(config.profiles[0], alerts=AlertSettings(ntfy_enabled=False))
    seam.seed_state(app.rt, config=replace(config, profiles=(profile,)))
    obligation = seam.obligation(
        app.rt, replace(alert(), kind=EventKind.KEY_REJECTED), None, clock.now()
    )
    remaining, sent = await deliver(
        obligation, seam.ledger(app.rt), app.notifier, clock.now(), app.notifier.destination_id
    )
    assert remaining is None and sent
    assert tg.sent[-1]["text"] == obligation.event.text


@pytest.mark.parametrize("pending", ["retry", "quiet", "clear", "rotated"])
async def test_ntfy_optout_survives_disruption_recomputation_and_restart(tmp_path, tg, pending):
    phone, webhook = [], []
    failing = pending in ("retry", "rotated")

    def receive(request):
        body = json.loads(request.content)
        if request.url.host == "copy.example.test":
            webhook.append(body["kind"])
        elif "Signal fault" in body["message"]:
            phone.append(body["message"])
            return httpx.Response(500 if failing else 200)
        return httpx.Response(200)

    tg.other = receive
    clock = TickClock(at("08:04"))
    provider = Provider(clock, [USUAL])
    incident = ActiveDisruption("incident", "Signal fault", frozenset({"UT"}), None)
    provider.disruptions = ActiveDisruptions((incident,), "complete")
    stations = StationsProvider(provider, CODES)
    app = make_app(
        tmp_path,
        tg,
        clock,
        provider=stations,
        ntfy=NtfySettings(TOPIC, TOKEN),
        bot_token="",
        chat_id=None,
        webhook_urls=("https://copy.example.test/hook",),
        webhook_secret="test-secret",
    )
    quiet = '    alerts: {quiet_hours: ["08:00", "08:07"]}\n'
    if pending == "quiet":
        app.settings.profiles.write_text(ONE_PROFILE + quiet)
    await app.rt.start()
    await app.rt.webhooks_idle()
    initial_count = 0 if pending == "quiet" else 1
    assert len(phone) == initial_count
    assert webhook.count("disruption") == initial_count

    if pending == "clear":
        clock.jump(2)
        failing = True
        provider.disruptions = ActiveDisruptions((), "complete")
        await app.rt.poll_once()
        await app.rt.webhooks_idle()
        assert len(phone) == 2
        assert webhook.count("disruption_cleared") == 1

    app.settings.profiles.write_text(ONE_PROFILE + "    alerts: {ntfy_enabled: false}\n")
    if pending == "quiet":
        app.settings.profiles.write_text(ONE_PROFILE + quiet.replace("}", ", ntfy_enabled: false}"))
    disabled = app
    if pending == "rotated":
        settings = replace(app.settings, ntfy=NtfySettings(TOPIC + "x", TOKEN))
        disabled = build(settings, clock=clock, provider=stations, client=tg.client())
        await disabled.rt.start()
    else:
        assert await app.rt.reload() is None
    await disabled.rt.webhooks_idle()
    assert not any(
        o.event.kind in (EventKind.DISRUPTION, EventKind.DISRUPTION_CLEARED)
        for o in disabled.rt.state.obligations
    )

    clock.jump(4)
    failing = False
    app.settings.profiles.write_text(ONE_PROFILE)
    restarted = build(app.settings, clock=clock, provider=stations, client=tg.client())
    await restarted.rt.start()
    await restarted.rt.webhooks_idle()
    expected_attempts = {"retry": 1, "quiet": 0, "clear": 2, "rotated": 1}[pending]
    assert len(phone) == expected_attempts
    assert webhook.count("disruption") == 1
    assert webhook.count("disruption_cleared") == (pending == "clear")
    notice = next(iter(restarted.rt.state.occurrences.values())).disruptions["incident"]
    receipt = notice.receipts.get(restarted.notifier.destination_id)
    if pending == "clear":
        assert receipt is not None and receipt.start_revision == 1 and receipt.clear_revision == 0
    else:
        assert receipt is None

    clock.jump(2)
    provider.disruptions = ActiveDisruptions(
        (replace(incident, title="Signal fault update"),), "complete"
    )
    await restarted.rt.poll_once()
    await restarted.rt.webhooks_idle()
    assert len(phone) == expected_attempts + 1 and "Signal fault update" in phone[-1]
    assert webhook.count("disruption") == 2


@pytest.mark.parametrize("after_optout", ["off", "reenabled", "removed"])
async def test_ntfy_optout_stops_remaining_parts_after_yaml_reload(tmp_path, tg, after_optout):
    entered, release = asyncio.Event(), asyncio.Event()
    parts = []
    long_text = "🚆" * 3100

    async def receive(request):
        message = json.loads(request.content)["message"]
        if message.startswith("🚆"):
            parts.append(message)
            if len(parts) == 1:
                entered.set()
                await release.wait()
        return httpx.Response(200)

    tg.handler = receive
    clock = TickClock(at("07:20"))
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN), bot_token="", chat_id=None)
    await app.rt.start()
    event = replace(alert(), kind=EventKind.KEY_REJECTED, text=long_text)
    obligation = seam.obligation(app.rt, event, None, clock.now())
    seam.seed_state(app.rt, obligations=(obligation,))
    sending = asyncio.create_task(seam.deliver(app.rt))
    reloads = []
    try:
        await asyncio.wait_for(entered.wait(), 2)
        app.settings.profiles.write_text(ONE_PROFILE + "    alerts: {ntfy_enabled: false}\n")
        reloads.append(asyncio.create_task(app.rt.reload()))
        await until(lambda: not seam.store(app.rt).load().config.profiles[0].alerts.ntfy_enabled)
        if after_optout != "off":
            app.settings.profiles.write_text(ONE_PROFILE)
            reloads.append(asyncio.create_task(app.rt.reload()))
            await until(lambda: seam.store(app.rt).load().config.profiles[0].alerts.ntfy_enabled)
        if after_optout == "removed":
            # A new look-ahead run can replace a pending bundle while its first part waits.
            seam.seed_state(app.rt, obligations=())
            seam.save_state(app.rt)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(sending, *reloads), 2)
    assert len(parts) == 1
    assert event not in app.rt.events_log
    assert not app.rt.state.obligations


def adoption(rt, *, text=None, start=NOW, later=False):
    key = direction_key("p1", OUT)
    base = BaselineHistory((confirmed(),))
    shifted = [
        result
        for day in DAYS
        for result in probe(
            day,
            base.in_force(day),
            l1=("08:19", "08:47") if later else ("08:15", "08:43"),
            l2=("08:53", "09:31") if later else ("08:49", "09:27"),
        )
    ]
    config = rt.config
    seam.seed_state(rt, baselines={key: base})
    seam.apply_lookahead_run(
        rt, config.profiles[0], OUT, ProbedRun(start, config.revision, base.latest, tuple(shifted))
    )
    ledger = rt.state.lookahead[key]
    old = next(o for o in ledger.pending if o.event.kind is EventKind.TIMETABLE_ADOPTED)
    if text is not None:
        updated = replace(old, event=replace(old.event, text=text))
        seam.seed_state(
            rt,
            lookahead={key: replace(ledger, pending=(updated,))},
            obligations=(updated,),
        )
        old = updated
    return rt.state.baselines[key], old


def withdraw_adoption(rt, history, now):
    results = tuple(result for day in DAYS for result in probe(day, history.in_force(day)))
    config = rt.config
    seam.apply_lookahead_run(
        rt, config.profiles[0], OUT, ProbedRun(now, config.revision, history.latest, results)
    )
    assert rt.state.baselines[direction_key("p1", OUT)].latest != history.latest


async def test_saved_ntfy_optout_outlives_lookahead_replacement_and_profile_removal(tmp_path, tg):
    parts = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def receive(request):
        message = json.loads(request.content)["message"]
        if message.startswith("🚆"):
            parts.append(message)
            entered.set()
            await release.wait()
        return httpx.Response(200)

    tg.handler = receive
    clock = TickClock(NOW)
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN))
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, old = adoption(app.rt, text="🚆" * 3100)
    sending = asyncio.create_task(seam.deliver(app.rt))
    tasks = [sending]
    store = seam.store(app.rt)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        app.settings.profiles.write_text(YAML + "    alerts: {ntfy_enabled: false}\n")
        tasks.append(asyncio.create_task(app.rt.reload()))
        await until(lambda: not store.load().config.profiles[0].alerts.ntfy_enabled)
        clock.jump(5)
        withdraw_adoption(app.rt, history, clock.now())
        assert all(o.id != old.id for o in store.load().obligations)
        app.settings.profiles.write_text(
            'profiles:\n  - id: "p2"' + PROFILE.split('  - id: "p2"')[1]
        )
        tasks.append(asyncio.create_task(app.rt.reload()))
        await until(lambda: all(p.id != "p1" for p in store.load().config.profiles))
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert len(parts) == 1
    assert old.event not in app.rt.events_log


async def test_ntfy_optout_saved_in_telegram_mode_cancels_pinned_disruption(tmp_path, tg):
    phone = []
    tg.other = lambda r: phone.append(json.loads(r.content)["message"]) or httpx.Response(200)
    clock = TickClock(at("08:04"))
    provider = Provider(clock, [USUAL])
    provider.disruptions = ActiveDisruptions(
        (ActiveDisruption("incident", "Signal fault", frozenset({"UT"}), None),), "complete"
    )
    stations = StationsProvider(provider, CODES)
    app = make_app(tmp_path, tg, clock, provider=stations, ntfy=NtfySettings(TOPIC, TOKEN))
    quiet = '    alerts: {quiet_hours: ["08:00", "08:07"]}\n'
    app.settings.profiles.write_text(ONE_PROFILE + quiet)
    await app.rt.start()
    queued = next(o for o in app.rt.state.obligations if o.event.kind is EventKind.DISRUPTION)
    assert queued.target_ids == {app.notifier.destination_id}

    telegram = build(
        replace(app.settings, ntfy=None), clock=clock, provider=stations, client=tg.client()
    )
    await telegram.rt.start()
    app.settings.profiles.write_text(ONE_PROFILE + quiet.replace("}", ", ntfy_enabled: false}"))
    assert await telegram.rt.reload() is None

    app.settings.profiles.write_text(ONE_PROFILE + quiet)
    clock.jump(4)
    resumed = build(app.settings, clock=clock, provider=stations, client=tg.client())
    await resumed.rt.start()
    assert not any("Signal fault" in message for message in phone)
    notice = next(iter(resumed.rt.state.occurrences.values())).disruptions["incident"]
    assert resumed.notifier.destination_id not in notice.receipts
    assert not any("Signal fault" in entry["text"] for entry in tg.sent)


async def test_ntfy_optout_keeps_quiet_alert_pinned_to_telegram(tmp_path, tg):
    phone = []
    tg.other = lambda r: phone.append(json.loads(r.content)["message"]) or httpx.Response(200)
    clock = TickClock(at("08:04"))
    app = make_app(tmp_path, tg, clock)
    quiet = '    alerts: {quiet_hours: ["08:00", "08:07"]}\n'
    app.settings.profiles.write_text(ONE_PROFILE + quiet)
    await app.rt.start()
    event = replace(alert(), kind=EventKind.TIMETABLE_ADOPTED, severity=Severity.SILENT)
    seam.seed_state(app.rt, obligations=(seam.obligation(app.rt, event, None, clock.now()),))
    await seam.deliver(app.rt)
    assert seam.store(app.rt).load().obligations[0].target_ids == {app.notifier.destination_id}

    app.settings.profiles.write_text(ONE_PROFILE + quiet.replace("}", ", ntfy_enabled: false}"))
    ntfy = build(
        replace(app.settings, ntfy=NtfySettings(TOPIC, TOKEN)),
        clock=clock,
        provider=seam.provider(app.rt),
        client=tg.client(),
    )
    await ntfy.rt.start()
    clock.jump(4)
    resumed = build(app.settings, clock=clock, provider=seam.provider(app.rt), client=tg.client())
    await resumed.rt.start()
    assert any(entry["text"] == event.text for entry in tg.sent)
    assert event.text not in phone


@pytest.mark.parametrize("kind", ["disruption", "disruption_cleared"])
async def test_ntfy_optout_keeps_webhook_disruption_retry_identity(tmp_path, tg, kind):
    failed, accepted, phone = [], [], []
    needle = "Signal fault" if kind == "disruption" else "no longer lists"

    def receive(request):
        body = json.loads(request.content)
        if request.url.host.startswith("copy"):
            if body["kind"] != kind:
                return httpx.Response(200)
            attempt = (request.headers["idempotency-key"], request.content)
            if request.url.host == "copy.example.test":
                failed.append(attempt)
                return httpx.Response(500 if len(failed) == 1 else 200)
            accepted.append(attempt)
            return httpx.Response(200)
        phone.append(body["message"])
        return httpx.Response(500 if needle in body["message"] else 200)

    tg.other = receive
    clock = TickClock(at("08:04"))
    provider = Provider(clock, [USUAL])
    provider.disruptions = ActiveDisruptions(
        (ActiveDisruption("incident", "Signal fault", frozenset({"UT"}), None),), "complete"
    )
    app = make_app(
        tmp_path,
        tg,
        clock,
        provider=StationsProvider(provider, CODES),
        ntfy=NtfySettings(TOPIC, TOKEN),
        webhook_urls=("https://copy.example.test/hook", "https://copy2.example.test/hook"),
        webhook_secret="test-secret",
    )
    await app.rt.start()
    await app.rt.webhooks_idle()
    if kind == "disruption_cleared":
        clock.jump(2)
        provider.disruptions = ActiveDisruptions((), "complete")
        await app.rt.poll_once()
        await app.rt.webhooks_idle()
    (copy,) = seam.owed_copies(app.rt).values()
    assert copy.first_try == copy.last_try == clock.now()
    assert len(copy.done) == 1 and failed == accepted and len(failed) == 1

    app.settings.profiles.write_text(ONE_PROFILE + "    alerts: {ntfy_enabled: false}\n")
    assert await app.rt.reload() is None
    await app.rt.webhooks_idle()
    assert tuple(seam.owed_copies(app.rt).values()) == (copy,)

    clock.jump(2)
    await app.rt.poll_once()
    await app.rt.webhooks_idle()
    assert failed == [accepted[0], accepted[0]] and len(accepted) == 1
    assert sum(needle in message for message in phone) == 1, phone
    assert not seam.owed_copies(app.rt)
    notice = next(iter(app.rt.state.occurrences.values())).disruptions["incident"]
    assert copy.target_ids <= notice.receipts.keys()
    receipt = notice.receipts.get(app.notifier.destination_id)
    if kind == "disruption":
        assert receipt is None
    else:
        assert receipt.start_revision == 1 and receipt.clear_revision == 0


@pytest.mark.parametrize("accepted", [False, True])
async def test_ntfy_timetable_correction_requires_accepted_adoption(tmp_path, tg, accepted):
    messages = []
    tg.other = lambda r: messages.append(json.loads(r.content)["message"]) or httpx.Response(200)
    clock = TickClock(NOW)
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN))
    quiet = '    alerts: {quiet_hours: ["05:55", "06:20"]}\n'
    app.settings.profiles.write_text(YAML + ("" if accepted else quiet))
    await app.rt.start()
    history, old = adoption(app.rt)
    await seam.deliver(app.rt)
    assert (old.event.text in messages) is accepted
    if not accepted:
        app.settings.profiles.write_text(YAML + "    alerts: {ntfy_enabled: false}\n")
        assert await app.rt.reload() is None
        assert all(o.id != old.id for o in seam.store(app.rt).load().obligations)
    app.settings.profiles.write_text(YAML)
    clock.jump(25)
    # Evidence of acceptance must survive a process restart.
    resumed = build(app.settings, clock=clock, provider=seam.provider(app.rt), client=tg.client())
    await resumed.rt.start()
    withdraw_adoption(resumed.rt, history, clock.now())
    await seam.deliver(resumed.rt)
    assert any("ignore the earlier change" in message for message in messages) is accepted


@pytest.mark.parametrize("use_ntfy", [False, True])
async def test_timetable_correction_reaches_webhook_that_received_adoption(tmp_path, tg, use_ntfy):
    messages, copies = [], []

    def receive(request):
        body = json.loads(request.content)
        if request.url.host == "copy.example.test":
            copies.append(body["text"])
            return httpx.Response(200)
        messages.append(body["message"])
        return httpx.Response(500 if "NS moved" in body["message"] else 200)

    tg.other = receive
    clock = TickClock(NOW)
    app = make_app(
        tmp_path,
        tg,
        clock,
        ntfy=NtfySettings(TOPIC, TOKEN) if use_ntfy else None,
        webhook_urls=("https://copy.example.test/hook",),
        webhook_secret="test-secret",
    )
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, old = adoption(app.rt)
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert old.event.text in copies
    assert (old.event in app.rt.events_log) is not use_ntfy

    clock.jump(25)
    withdraw_adoption(app.rt, history, clock.now())
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert any("ignore the earlier change" in message for message in copies)
    assert not any("ignore the earlier change" in message for message in messages)
    assert any("ignore the earlier change" in entry["text"] for entry in tg.sent) is not use_ntfy


async def test_legacy_adoption_with_unknown_audience_cannot_reach_new_webhook(tmp_path, tg):
    messages = []

    def receive(request):
        messages.append(request.content.decode())
        return httpx.Response(200)

    tg.other = receive
    clock = TickClock(NOW)
    app = make_app(tmp_path, tg, clock)
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, old = adoption(app.rt)
    await seam.deliver(app.rt)
    assert old.event in app.rt.events_log
    key = direction_key("p1", OUT)
    ledger = replace(app.rt.state.lookahead[key], adoption_receipts={})
    seam.seed_state(app.rt, lookahead={**app.rt.state.lookahead, key: ledger})
    seam.save_state(app.rt)

    clock.jump(25)
    resumed = build(
        replace(
            app.settings,
            ntfy=NtfySettings(TOPIC, TOKEN),
            webhook_urls=("https://copy.example.test/hook",),
            webhook_secret="test-secret",
        ),
        clock=clock,
        provider=seam.provider(app.rt),
        client=tg.client(),
    )
    await resumed.rt.start()
    withdraw_adoption(resumed.rt, history, clock.now())
    await seam.deliver(resumed.rt)
    await resumed.rt.webhooks_idle()
    assert not any("ignore the earlier change" in message for message in messages)


async def test_failed_receipt_save_still_allows_owed_webhook_correction(tmp_path, tg, monkeypatch):
    copies = []
    fail_save = False

    def receive(request):
        nonlocal fail_save
        if request.url.host == "copy.example.test":
            copies.append(json.loads(request.content)["text"])
            if "NS moved" in copies[-1]:
                fail_save = True
        return httpx.Response(200)

    tg.other = receive
    clock = TickClock(NOW)
    app = make_app(
        tmp_path,
        tg,
        clock,
        ntfy=NtfySettings(TOPIC, TOKEN),
        webhook_urls=("https://copy.example.test/hook",),
        webhook_secret="test-secret",
    )
    app.settings.profiles.write_text(YAML + "    alerts: {ntfy_enabled: false}\n")
    await app.rt.start()
    history, old = adoption(app.rt)
    store = seam.store(app.rt)
    save = store.save

    def save_or_fail(state):
        if fail_save:
            raise StorageError("test disk failure after webhook acceptance")
        save(state)

    monkeypatch.setattr(store, "save", save_or_fail)
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert fail_save and old.event.text in copies
    assert old.event not in app.rt.events_log
    key = direction_key("p1", OUT)
    assert not any(store.load().lookahead[key].adoption_receipts.values())

    clock.jump(25)
    config = app.rt.config
    results = tuple(result for day in DAYS for result in probe(day, history.in_force(day)))
    seam.apply_lookahead_run(
        app.rt,
        config.profiles[0],
        OUT,
        ProbedRun(clock.now(), config.revision, history.latest, results),
    )
    fail_save = False
    seam.save_state(app.rt)
    assert app.rt.state.baselines[key].latest != history.latest
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert sum("ignore the earlier change" in message for message in copies) == 1


async def test_timetable_correction_reaches_phone_after_adoption_inflight_at_withdrawal(
    tmp_path, tg
):
    entered, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def receive(request):
        message = json.loads(request.content)["message"]
        if "NS moved" in message:
            entered.set()
            await release.wait()
        messages.append(message)
        return httpx.Response(200)

    tg.handler = receive
    clock = TickClock(NOW)
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN))
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, _ = adoption(app.rt)
    sending = asyncio.create_task(seam.deliver(app.rt))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clock.jump(5)
        withdraw_adoption(app.rt, history, clock.now())
    finally:
        release.set()
        await asyncio.wait_for(sending, 2)
    await seam.deliver(app.rt)
    assert any("ignore the earlier change" in message for message in messages)


def legacy_adoption_file(app):
    path = app.settings.state_dir / "tcc-state.json"
    raw = json.loads(path.read_text())
    for ledger in raw["state"]["lookahead"].values():
        ledger.pop("adoption_ids", None)
        ledger.pop("adoption_receipts", None)
    path.write_text(json.dumps(raw))


async def test_late_adoption_acceptance_does_not_prove_recreated_profile(tmp_path, tg):
    entered, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def receive(request):
        message = json.loads(request.content)["message"]
        if "08:15" in message and "NS moved" in message:
            entered.set()
            await release.wait()
        messages.append(message)
        if "NS moved" in message and "08:19" in message:
            return httpx.Response(500)
        return httpx.Response(200)

    tg.handler = receive
    clock = TickClock(NOW)
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN))
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    before, old = adoption(app.rt)
    store = seam.store(app.rt)
    sending = asyncio.create_task(seam.deliver(app.rt))
    reloads = []
    try:
        await asyncio.wait_for(entered.wait(), 2)
        app.settings.profiles.write_text("profiles: []\n")
        reloads.append(asyncio.create_task(app.rt.reload()))
        await until(lambda: not store.load().config.profiles)
        revision = store.load().config.revision
        app.settings.profiles.write_text(YAML)
        reloads.append(asyncio.create_task(app.rt.reload()))
        await until(lambda: store.load().config.revision > revision)
        clock.jump(5)
        after, new = adoption(app.rt, start=NOW, later=True)
        assert before.latest == after.latest
        assert old.id != new.id
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(sending, *reloads), 2)
    assert any("08:15" in message for message in messages)
    clock.jump(25)
    withdraw_adoption(app.rt, after, clock.now())
    await seam.deliver(app.rt)
    assert not any("ignore the earlier change" in message for message in messages)


@pytest.mark.parametrize(
    "continuity", ["same", "same_pinned", "replacement", "reconfirmed", "repinned", "unknown"]
)
async def test_legacy_telegram_correction_requires_saved_continuity(tmp_path, tg, continuity):
    clock = TickClock(NOW)
    pinned = continuity in ("same_pinned", "repinned")
    if pinned:
        tg.other = lambda _: httpx.Response(500)
    app = make_app(
        tmp_path,
        tg,
        clock,
        webhook_urls=("https://owed.example.test/hook",) if pinned else (),
        webhook_secret="test-secret" if pinned else "",
    )
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, old = adoption(app.rt)
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert old.event in app.rt.events_log
    if pinned:
        (copy,) = seam.store(app.rt).load().webhook_copies
        assert copy.obligation.id == old.id
        assert any(x.startswith("telegram:") for x in copy.obligation.target_ids)
    legacy_adoption_file(app)
    if continuity != "unknown":
        confirmed_chat = (
            STRANGER if continuity in ("reconfirmed", "repinned") else app.settings.chat_id
        )
        save_state(
            tmp_path / "telegram.json",
            ShellState(bot_id="123", confirmed_for=f"123:{confirmed_chat}"),
        )
    settings = (
        replace(app.settings, chat_id=STRANGER)
        if continuity in ("replacement", "reconfirmed", "repinned")
        else app.settings
    )
    clock.jump(25)
    resumed = build(settings, clock=clock, provider=seam.provider(app.rt), client=tg.client())
    await resumed.rt.start()
    withdraw_adoption(resumed.rt, history, clock.now())
    await seam.deliver(resumed.rt)
    corrections = [x for x in tg.sent if "ignore the earlier change" in x["text"]]
    assert [x["chat_id"] for x in corrections] == (
        [app.settings.chat_id] if continuity == "same_pinned" else []
    )


async def test_queued_correction_waits_for_inflight_acceptance_to_be_saved(
    tmp_path, tg, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    messages = []
    fail_save = False

    async def receive(request):
        nonlocal fail_save
        message = json.loads(request.content)["message"]
        if "NS moved" in message:
            entered.set()
            await release.wait()
            fail_save = True
        messages.append(message)
        return httpx.Response(200)

    tg.handler = receive
    clock = TickClock(NOW)
    app = make_app(tmp_path, tg, clock, ntfy=NtfySettings(TOPIC, TOKEN))
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, _ = adoption(app.rt)
    store = seam.store(app.rt)
    save = store.save

    def save_or_fail(state):
        if fail_save:
            raise StorageError("test disk failure after acceptance")
        save(state)

    monkeypatch.setattr(store, "save", save_or_fail)
    sending = asyncio.create_task(seam.deliver(app.rt))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clock.jump(5)
        withdraw_adoption(app.rt, history, clock.now())
        queued = next(
            o for o in app.rt.state.obligations if "ignore the earlier change" in o.event.text
        )
        key = direction_key("p1", OUT)
        ledger = app.rt.state.lookahead[key]
        queued = replace(queued, target_ids=frozenset({app.notifier.destination_id}))
        seam.seed_state(
            app.rt,
            obligations=(queued,),
            lookahead={key: replace(ledger, pending=(queued,))},
        )
        seam.save_state(app.rt)
    finally:
        release.set()
        await asyncio.wait_for(sending, 2)
    await seam.deliver(app.rt)
    assert not any("ignore the earlier change" in x for x in messages)
    fail_save = False
    seam.save_state(app.rt)
    persisted = next(o for o in store.load().obligations if o.id == queued.id)
    assert persisted.target_ids == queued.target_ids
    resumed = build(app.settings, clock=clock, provider=seam.provider(app.rt), client=tg.client())
    await resumed.rt.start()
    await seam.deliver(resumed.rt)
    assert sum("ignore the earlier change" in x for x in messages) == 1


async def test_queued_webhook_correction_waits_for_durable_receipt(tmp_path, tg, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    corrections = []
    fail_save = False

    async def receive(request):
        nonlocal fail_save
        payload = json.loads(request.content)
        text = payload.get("text", "")
        if "NS moved" in text:
            entered.set()
            await release.wait()
            fail_save = True
        if "ignore the earlier change" in text:
            corrections.append((request.url.host, request.headers["webhook-id"]))
        return httpx.Response(200)

    tg.handler = receive
    clock = TickClock(NOW)
    app = make_app(
        tmp_path,
        tg,
        clock,
        ntfy=NtfySettings(TOPIC, TOKEN),
        webhook_urls=("https://copy.example.test/hook",),
        webhook_secret="test-secret",
    )
    app.settings.profiles.write_text(YAML + "    alerts: {ntfy_enabled: false}\n")
    await app.rt.start()
    history, _ = adoption(app.rt)
    store = seam.store(app.rt)
    save = store.save

    def save_or_fail(state):
        if fail_save:
            raise StorageError("test disk failure after webhook acceptance")
        save(state)

    monkeypatch.setattr(store, "save", save_or_fail)
    await seam.deliver(app.rt)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clock.jump(5)
        withdraw_adoption(app.rt, history, clock.now())
        await seam.deliver(app.rt)
        queued = next(
            c
            for c in store.load().webhook_copies
            if "ignore the earlier change" in c.obligation.event.text
        )
        assert queued.target_ids and queued.first_try is None
    finally:
        release.set()
        await app.rt.webhooks_idle()
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert not corrections
    assert queued.id in seam.owed_copies(app.rt)
    fail_save = False
    seam.save_state(app.rt)
    persisted = next(c for c in store.load().webhook_copies if c.id == queued.id)
    assert persisted.id == queued.id and persisted.target_ids == queued.target_ids
    resumed = build(
        replace(
            app.settings, webhook_urls=(*app.settings.webhook_urls, "https://new.example.test/hook")
        ),
        clock=clock,
        provider=seam.provider(app.rt),
        client=tg.client(),
    )
    await resumed.rt.start()
    await resumed.rt.webhooks_idle()
    assert corrections == [("copy.example.test", queued.id)]


async def test_unsaved_return_edit_does_not_hide_outbound_acceptance(tmp_path, tg, monkeypatch):
    messages = []
    fail_save = False

    def receive(request):
        nonlocal fail_save
        if request.url.host == "copy.example.test":
            messages.append(json.loads(request.content)["text"])
            if "NS moved" in messages[-1]:
                fail_save = True
        return httpx.Response(200)

    tg.other = receive
    clock = TickClock(NOW)
    app = make_app(
        tmp_path,
        tg,
        clock,
        ntfy=NtfySettings(TOPIC, TOKEN),
        webhook_urls=("https://copy.example.test/hook",),
        webhook_secret="test-secret",
    )
    yaml = YAML + "    alerts: {ntfy_enabled: false}\n"
    app.settings.profiles.write_text(yaml)
    await app.rt.start()
    history, _ = adoption(app.rt)
    store = seam.store(app.rt)
    save = store.save

    def save_or_fail(state):
        if fail_save:
            raise StorageError("test disk failure")
        save(state)

    monkeypatch.setattr(store, "save", save_or_fail)
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert fail_save
    app.settings.profiles.write_text(yaml.replace("17:52", "17:58"))
    assert await app.rt.reload() is not None
    clock.jump(25)
    config = app.rt.config
    results = tuple(result for day in DAYS for result in probe(day, history.in_force(day)))
    seam.apply_lookahead_run(
        app.rt,
        config.profiles[0],
        OUT,
        ProbedRun(clock.now(), config.revision, history.latest, results),
    )
    fail_save = False
    seam.save_state(app.rt)
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    assert sum("ignore the earlier change" in x for x in messages) == 1


@pytest.mark.parametrize("exact_adoption", [True, False])
async def test_legacy_owed_copy_recovers_only_its_pinned_receipt(tmp_path, tg, exact_adoption):
    corrections = []

    def receive(request):
        payload = json.loads(request.content)
        if "ignore the earlier change" in payload["text"]:
            corrections.append(request.url.host)
        return httpx.Response(200 if request.url.host == "done.example.test" else 500)

    tg.other = receive
    clock = TickClock(NOW)
    app = make_app(
        tmp_path,
        tg,
        clock,
        webhook_urls=("https://done.example.test/hook", "https://owed.example.test/hook"),
        webhook_secret="test-secret",
    )
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, old = adoption(app.rt)
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    (copy,) = seam.store(app.rt).load().webhook_copies
    assert copy.obligation.id == old.id and len(copy.done) == 1
    legacy_adoption_file(app)
    if not exact_adoption:
        path = tmp_path / "tcc-state.json"
        raw = json.loads(path.read_text())
        raw["state"]["webhook_copies"][0]["obligation"]["id"] += "|old instance"
        path.write_text(json.dumps(raw))
    clock.jump(25)
    resumed = build(app.settings, clock=clock, provider=seam.provider(app.rt), client=tg.client())
    await resumed.rt.start()
    withdraw_adoption(resumed.rt, history, clock.now())
    await seam.deliver(resumed.rt)
    await resumed.rt.webhooks_idle()
    assert corrections == (["done.example.test"] if exact_adoption else [])


async def test_webhook_correction_key_matches_filtered_facts_and_keeps_retry_id(tmp_path, tg):
    attempts = []

    def receive(request):
        payload = json.loads(request.content)
        if "Other timetable news" in payload["text"]:
            attempts.append(
                (payload, request.headers["webhook-id"], request.headers["idempotency-key"])
            )
        return httpx.Response(500 if len(attempts) == 1 else 200)

    tg.other = receive
    clock = TickClock(NOW)
    app = make_app(
        tmp_path,
        tg,
        clock,
        webhook_urls=("https://copy.example.test/hook",),
        webhook_secret="test-secret",
    )
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, _ = adoption(app.rt)
    config = app.rt.config
    key = direction_key("p1", OUT)
    clock.jump(5)
    withdraw_adoption(app.rt, history, clock.now())
    ledger = app.rt.state.lookahead[key]
    (correction,) = ledger.pending
    correction_fact = ledger.facts[correction.id][0]
    news = LookAheadFact("test-news", "Other timetable news", False, False)
    facts = (correction_fact, news)
    kind, body = render_message(config.profiles[0], correction.look_ahead[2], facts)
    mixed = replace(
        correction,
        fact_key=bundle_key(facts),
        event=replace(
            correction.event, kind=kind, text=body, key=f"{correction.event.key};{news.key}"
        ),
    )
    seam.seed_state(
        app.rt,
        lookahead={key: replace(ledger, facts={mixed.id: facts}, pending=(mixed,))},
        obligations=(mixed,),
    )
    seam.save_state(app.rt)
    await seam.deliver(app.rt)
    await app.rt.webhooks_idle()
    clock.jump(61)
    resumed = build(app.settings, clock=clock, provider=seam.provider(app.rt), client=tg.client())
    await resumed.rt.start()
    await resumed.rt.webhooks_idle()
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    payload, webhook_id, idempotency_key = attempts[0]
    assert payload["key"] == mixed.event.key.removesuffix(mixed.fact_key) + news.key
    assert "ignore the earlier change" not in payload["text"]
    assert idempotency_key == webhook_id


@pytest.mark.parametrize("withdraw", [False, True])
async def test_timetable_receipts_prune_after_last_use(tmp_path, tg, withdraw):
    clock = TickClock(NOW)
    app = make_app(tmp_path, tg, clock)
    app.settings.profiles.write_text(YAML)
    await app.rt.start()
    history, _ = adoption(app.rt)
    await seam.deliver(app.rt)
    key = direction_key("p1", OUT)
    assert any(app.rt.state.lookahead[key].adoption_receipts.values())
    if withdraw:
        clock.jump(25)
        withdraw_adoption(app.rt, history, clock.now())
        assert any(app.rt.state.lookahead[key].adoption_receipts.values())
        await seam.deliver(app.rt)
    else:
        clock.t += timedelta(days=2)
        seam.save_state(app.rt)
    assert not seam.store(app.rt).load().lookahead[key].adoption_receipts


async def test_long_unicode_messages_resume_only_unaccepted_parts():
    bodies = []

    def receive(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(500 if len(bodies) == 2 else 200)

    clock = TickClock(at("08:00"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        notifier = NtfyNotifier(NtfySettings(TOPIC, TOKEN), client, clock.now)
        event = replace(alert(), text="🚆" * 2100)
        assert not await notifier.send(event)
        clock.jump(1)
        assert await notifier.send(event)
        assert len(bodies) == 4
        assert all(len(b["message"].encode()) <= 4096 for b in bodies)
        assert "".join(b["message"] for i, b in enumerate(bodies) if i != 1) == event.text


def codes(bodies):
    return json.dumps(
        {
            name: body["message"].rsplit(": ", 1)[1]
            for name, body in zip(("loud", "quiet"), bodies[-2:], strict=True)
        }
    ).encode()


async def test_proof_requires_both_fresh_phone_codes_and_persists_only_destination(
    tmp_path, monkeypatch
):
    bodies = []
    clock = TickClock(at("08:00"))
    path = tmp_path / "ntfy-proof.json"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200)
        )
    ) as client:
        notifier = NtfyNotifier(NtfySettings(TOPIC, TOKEN), client, clock.now)
        proof = NtfyProof(notifier, path, clock.now)
        assert not proof.confirmed
        assert (await proof.test())[0] == 200 and not proof.confirmed
        stale = codes(bodies)
        assert (await NtfyProof(notifier, path, clock.now).confirm(stale))[0] == 409
        assert (await proof.confirm(b'{"loud":"0000000000000000","quiet":"0000000000000000"}'))[
            0
        ] == 409
        assert (await proof.confirm(b'{"loud":123}'))[0] == 400
        clock.jump(15)
        assert (await proof.confirm(stale))[0] == 409
        assert (await proof.test())[0] == 200
        assert (await proof.confirm(stale))[0] == 409
        fresh = codes(bodies)
        import tcc_app.ntfy as module

        real_replace = module.os.replace
        monkeypatch.setattr(
            module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError(TOKEN))
        )
        assert (await proof.confirm(fresh))[0] == 503 and not proof.confirmed
        assert list(tmp_path.iterdir()) == []
        monkeypatch.setattr(module.os, "replace", real_replace)
        assert (await proof.confirm(fresh)) == (200, {"delivery_confirmed": True})
        assert proof.confirmed and NtfyProof(notifier, path, clock.now).confirmed
        assert (await proof.confirm(fresh))[0] == 409
        assert TOKEN not in path.read_text() and TOPIC not in path.read_text()
        assert fresh.decode() not in path.read_text()
        assert path.stat().st_mode & 0o077 == 0
        for settings in (
            NtfySettings(TOPIC + "x", TOKEN),
            NtfySettings(TOPIC, TOKEN + "x"),
            NtfySettings(TOPIC, TOKEN, "https://own.invalid"),
        ):
            changed = NtfyNotifier(settings, client, clock.now)
            assert not NtfyProof(changed, path, clock.now).confirmed


async def test_failed_test_pair_cannot_be_confirmed(tmp_path):
    bodies = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                bodies.append(json.loads(r.content))
                or httpx.Response(500 if len(bodies) == 2 else 200)
            )
        )
    ) as client:
        clock = TickClock(at("08:00"))
        proof = NtfyProof(
            NtfyNotifier(NtfySettings(TOPIC, TOKEN), client, clock.now),
            tmp_path / "proof",
            clock.now,
        )
        assert (await proof.test())[0] == 502
        assert (await proof.confirm(codes(bodies)))[0] == 409 and not proof.confirmed


async def test_cancelled_confirmation_keeps_and_drains_its_atomic_save(tmp_path, monkeypatch):
    bodies = []
    entered, release = threading.Event(), threading.Event()
    clock = TickClock(at("08:00"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200)
        )
    ) as client:
        proof = NtfyProof(
            NtfyNotifier(NtfySettings(TOPIC, TOKEN), client, clock.now),
            tmp_path / "proof",
            clock.now,
        )
        await proof.test()
        real = proof._save

        def delayed():
            entered.set()
            assert release.wait(5)
            return real()

        monkeypatch.setattr(proof, "_save", delayed)
        confirmation = asyncio.create_task(proof.confirm(codes(bodies)))
        try:
            await until(entered.is_set)
            confirmation.cancel()
            await asyncio.gather(confirmation, return_exceptions=True)
            assert proof.writes and not proof.confirmed
            closing = asyncio.create_task(proof.close())
            await asyncio.sleep(0)
            assert not closing.done()
        finally:
            release.set()
            await proof.close()
        await closing
        assert proof.confirmed


async def post(port, path, body=b"", token=HEALTH):
    authorization = f"Authorization: Bearer {token}\r\n" if token else ""
    return await ask(
        port,
        f"POST {path} HTTP/1.1\r\n{authorization}Content-Length: {len(body)}\r\n\r\n".encode()
        + body,
    )


async def test_ntfy_only_started_app_health_authenticated_proof_and_private_state(tmp_path, tg):
    bodies = []
    tg.other = lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200)
    clock = TickClock(at("07:50"))
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(ONE_PROFILE + "    alerts: {ntfy_enabled: false}\n")
    settings = Settings.from_env(
        {
            **ENV,
            "TCC_PROFILES": str(profiles),
            "TCC_STATE_DIR": str(tmp_path),
            "TCC_HEALTH_TOKEN": HEALTH,
            "TCC_MCP_AGENTS": json.dumps({"fixture": "a" * 32}),
        }
    )
    app = build(
        settings,
        clock=clock,
        provider=StationsProvider(Provider(clock, []), CODES),
        client=tg.client(),
    )
    assert app.bot is None and not app.confirmed and app.ntfy_proof is not None
    assert app.travel_actions is not None
    assert app.travel_actions.agent("a" * 32) is not None
    assert app.travel_actions.agent("b" * 32) is None
    refused = await dispatch_mcp(app, {"tool": "change_travel_plan"}, "a" * 32)
    assert refused["status"] == "denied" and "Telegram" in refused["text"]
    port = free_port()
    task = asyncio.create_task(serve(app, port))
    try:
        await until(lambda: app.rt.started)
        assert (await ask(port, b"GET /health HTTP/1.1\r\n\r\n"))[0] == 503
        assert (await ask(port, b"GET /today HTTP/1.1\r\n\r\n"))[0] == 401
        code, today = await ask(
            port, f"GET /today HTTP/1.1\r\nAuthorization: Bearer {HEALTH}\r\n\r\n".encode()
        )
        assert code == 200 and b'rel="manifest"' in today
        assert (await post(port, "/delivery/test", token=None))[0] == 401
        assert (await post(port, "/delivery/test", token="wrong"))[0] == 401
        basic = base64.b64encode(("tcc:" + HEALTH).encode()).decode()
        assert (
            await ask(
                port,
                f"POST /delivery/test HTTP/1.1\r\nAuthorization: Basic {basic}\r\n\r\n".encode(),
            )
        )[0] == 401
        assert (
            await ask(
                port,
                f"GET /delivery/test HTTP/1.1\r\nAuthorization: Bearer {HEALTH}\r\n\r\n".encode(),
            )
        )[0] == 404
        status, body = await post(port, "/delivery/test")
        assert status == 200 and not app.confirmed
        assert not any(b["message"].rsplit(": ", 1)[1].encode() in body for b in bodies[-2:])
        status, _ = await post(port, "/delivery/confirm", codes(bodies))
        assert status == 200 and app.confirmed
        report = health_report(app.rt.view(), True, app.confirmed)
        assert (await ask(port, b"GET /health HTTP/1.1\r\n\r\n"))[0] == 200
        assert report[1]["delivery_confirmed"] is True
        assert not tg.calls
        for path in tmp_path.glob("*.json"):
            assert TOPIC not in path.read_text() and TOKEN not in path.read_text()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert app.phase == "stopped" and not tg.calls


async def test_telegram_proof_cannot_confirm_ntfy_and_disabled_ntfy_never_sends(tmp_path, tg):
    from tcc_app.bot import ShellState, save_state

    save_state(tmp_path / "telegram.json", ShellState(bot_id="123", confirmed_for="123:1000001"))
    app = make_app(tmp_path, tg, TickClock(at("07:50")), ntfy=NtfySettings(TOPIC, TOKEN))
    assert app.bot.confirmed and not app.confirmed
    assert app.notifier.destination_id.startswith("ntfy:")
    assert not tg.calls
    disabled = make_app(tmp_path, tg, TickClock(at("07:50")), ntfy=None)
    assert disabled.confirmed and disabled.notifier.destination_id.startswith("telegram:")
    server = await start_web(
        0, health=lambda: (200, {"ok": True}), host="127.0.0.1", health_token=HEALTH
    )
    try:
        assert (await post(server.sockets[0].getsockname()[1], "/delivery/test"))[0] == 404
        assert (await post(server.sockets[0].getsockname()[1], "/delivery/confirm"))[0] == 404
    finally:
        server.close()
        await server.wait_closed()
    await app.bot.close()
    await disabled.bot.close()

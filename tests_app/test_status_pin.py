import json
import stat
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from app_rig import CHAT, ONE_PROFILE, STRANGER, TOKEN, error, occ, ok, view
from runtime_rig import FakeClock, Rig, at
from tcc_app import bot as bot_module
from tcc_app.bot import Bot, ShellState, load_state, save_state
from tcc_app.telegram import TelegramApi

from train_commute_companion.model import AlertSettings, Config
from train_commute_companion.profile import parse_profiles
from train_commute_companion.serde import decode, encode
from train_commute_companion.view import Moved, WorksDay


class StatusRuntime:
    def __init__(self):
        profiles, problems = parse_profiles(ONE_PROFILE, None)
        assert not problems
        self.config = Config(1, tuple(profiles))
        self.snapshot = view(occ())
        self.started = True
        self.clock = FakeClock(at("07:55"))

    def now(self):
        return self.clock.now()

    def view(self):
        return self.snapshot

    def advance(self, seconds=61):
        self.clock.jump(seconds / 60)


class StatusTelegram:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.pins = {CHAT: {20}}
        self.failure = {}
        self.next_id = 30
        self.chat_type = "private"
        self.before = lambda method: None

    def handle(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        self.calls.append((method, params))
        self.before(method)
        if method in self.failure:
            return self.failure.pop(method)
        chat = params["chat_id"]
        pins = self.pins.setdefault(chat, set())
        key = (chat, params.get("message_id"))
        if method == "getChat":
            return ok(
                {
                    "id": chat,
                    "type": self.chat_type,
                    "pinned_message": {"message_id": max(pins)} if pins else None,
                }
            )
        if method == "sendMessage":
            self.next_id += 1
            self.messages[chat, self.next_id] = params["text"]
            return ok({"message_id": self.next_id, "chat": {"id": chat, "type": "private"}})
        if method == "unpinChatMessage":
            if params["message_id"] not in pins:
                return error(400, "message is not pinned")
            pins.remove(params["message_id"])
            return ok(True)
        if method == "pinChatMessage":
            if key not in self.messages:
                return error(400, "message to pin not found")
            pins.add(params["message_id"])
            return ok(True)
        assert method == "editMessageText"
        if key not in self.messages:
            return error(400, "message to edit not found")
        if self.messages[key] == params["text"]:
            return error(400, "message is not modified")
        self.messages[key] = params["text"]
        return ok({"message_id": key[1]})

    def methods(self):
        return [method for method, _ in self.calls]


@pytest.fixture
async def rig(tmp_path):
    api = StatusTelegram()
    rt = StatusRuntime()
    path = tmp_path / "telegram.json"
    save_state(path, ShellState(bot_id="123", confirmed_for=f"123:{CHAT}"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handle)) as client:

        def bot(chat=CHAT, token=TOKEN):
            return Bot(TelegramApi(token, client), rt, chat, path)

        yield rt, api, path, bot


async def test_quiet_pin_edits_only_the_same_message_for_meaningful_changes(rig):
    rt, api, path, bot = rig
    owner = bot()
    await owner.refresh_status_pin()
    assert api.methods() == ["getChat", "sendMessage", "pinChatMessage"]
    initial = api.messages[CHAT, 31]
    assert "Office A" in initial and "platform 5" in initial and "+3 min" in initial
    assert "train " in initial
    assert api.calls[1][1]["disable_notification"] and api.calls[1][1]["protect_content"]
    assert api.calls[2][1] == {"chat_id": CHAT, "message_id": 31, "disable_notification": True}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert TOKEN not in path.read_text() and "Office A" not in path.read_text()

    now = rt.now() + timedelta(minutes=1)
    journey = replace(occ(), health=replace(occ().health, last_ok_at=now))
    rt.snapshot = replace(rt.snapshot, at=now, requests=10, occurrences=(journey,), undelivered=2)
    rt.advance()
    await owner.refresh_status_pin()
    assert api.methods().count("editMessageText") == 0

    leg = replace(journey.legs[0], departure_platform="9", train_number="5678")
    rt.snapshot = replace(rt.snapshot, occurrences=(replace(journey, legs=(leg,)),))
    rt.advance()
    await owner.refresh_status_pin()
    assert api.methods()[-1] == "editMessageText"
    assert api.calls[-1][1]["message_id"] == 31
    assert "platform 9" in api.messages[CHAT, 31] and "train 5678" in api.messages[CHAT, 31]
    assert api.pins[CHAT] == {20, 31}

    api.calls.clear()
    await bot().refresh_status_pin()
    assert api.methods() == ["getChat"]
    assert api.pins[CHAT] == {20, 31}


async def test_unconfirmed_unbound_changed_or_nonprivate_destination_receives_no_content(rig):
    _, api, path, bot = rig
    await bot().refresh_status_pin()
    api.calls.clear()
    await bot(STRANGER).refresh_status_pin()
    await bot(token="456:TEST").refresh_status_pin()
    await bot(None).refresh_status_pin()
    await bot(-100).refresh_status_pin()
    assert not api.calls
    shell = load_state(path)
    shell.confirmed_for = f"123:{STRANGER}"
    save_state(path, shell)
    api.chat_type = "group"
    await bot(STRANGER).refresh_status_pin()
    assert api.methods() == ["getChat"] and api.pins[CHAT] == {20, 31}
    api.chat_type = "private"
    await bot(STRANGER).refresh_status_pin()
    assert api.pins[CHAT] == {20, 31} and api.pins[STRANGER] == {32}


async def test_mixed_and_disabled_profiles_filter_or_withdraw_the_pin(rig):
    rt, api, _, bot = rig
    only = rt.config.profiles[0]
    other = replace(only, id="p2", name="secret_route")
    rt.config = replace(rt.config, profiles=(only, other))
    secret = occ("p2", profile_name="secret_route")
    rt.snapshot = view(
        occ(),
        secret,
        moved=(Moved("p2", secret.key.direction, "secret move"),),
        refusal="secret profile input",
    )
    owner = bot()
    await owner.refresh_status_pin()
    assert "Secret Route" in api.messages[CHAT, 31]
    rt.config = replace(
        rt.config, profiles=(only, replace(other, alerts=AlertSettings(telegram_status_pin=False)))
    )
    rt.advance()
    await owner.refresh_status_pin()
    assert "secret" not in api.messages[CHAT, 31].lower()
    assert "Office A" in api.messages[CHAT, 31]

    rt.config = replace(rt.config, profiles=())
    rt.advance()
    await owner.refresh_status_pin()
    assert api.methods()[-2:] == ["editMessageText", "unpinChatMessage"]
    assert "Office A" not in api.messages[CHAT, 31] and api.pins[CHAT] == {20}
    api.calls.clear()
    await bot().refresh_status_pin()
    assert not api.calls


@pytest.mark.parametrize(
    "empty",
    [
        "profiles: []",
        '# Remove all travellers\n"profiles": &none !!seq []',
        "defaults: {notes: keep}\nprofiles: []",
    ],
)
async def test_reload_of_empty_profiles_withdraws_pin_but_invalid_yaml_does_not(
    rig, tmp_path, empty
):
    _, api, path, _ = rig
    running = Rig(tmp_path, now="07:55")
    running.write(text=ONE_PROFILE)
    await running.rt.start()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(api.handle)) as client:
            owner = Bot(TelegramApi(TOKEN, client), running.rt, CHAT, path)
            await owner.refresh_status_pin()
            assert api.pins[CHAT] == {20, 31}

            for invalid in (
                "profiles: [",
                "profiles: [null]",
                "profiles: null",
                ONE_PROFILE + "profiles: []\n",
                "defaults: &defaults {profiles: [null], profiles: []}\n<<: *defaults",
                "<<: {profiles: [null]}\n<<: {profiles: []}",
                "<<: [ {profiles: [null], profiles: []} ]",
                "defaults: &defaults {profiles: [null], !!value profiles: []}\n<<: *defaults",
                "profiles: !!omap []",
                "profiles: !!pairs []",
                "profiles: [null]\n!!timestamp profiles: []",
                "<<: !!omap [{profiles: []}]",
                "<<: !!pairs [{profiles: []}]",
                "<<: !unsupported {profiles: []}",
                "defaults: &empty []\nprofiles: *empty",
                "metadata: &profile_key profiles\n*profile_key: []",
                "<<: {profiles: [null]}\nprofiles: []",
            ):
                running.write(text=invalid)
                await running.rt.reload()
                running.clock.jump(1)
                await owner.refresh_status_pin()
                assert api.pins[CHAT] == {20, 31}

            running.write(text=empty)
            await running.rt.reload()
            running.clock.jump(1)
            await owner.refresh_status_pin()
            assert api.pins[CHAT] == {20}
            assert api.messages[CHAT, 31] == "Pinned commute status is off."
    finally:
        await running.rt.stop_works_digest()
        await running.rt.recovery_idle()


async def test_lost_edit_receipt_is_repaired_after_opt_out_and_restart(rig):
    rt, api, _, bot = rig
    await bot().refresh_status_pin()
    before = api.messages[CHAT, 31]
    config = rt.config
    only = config.profiles[0]
    other = replace(only, id="p2", name="secret_route")
    rt.config = replace(config, profiles=(only, other))
    rt.snapshot = view(occ(), occ("p2", profile_name="secret_route"))

    def lose_receipt(method):
        if method == "editMessageText":
            api.messages[CHAT, 31] = api.calls[-1][1]["text"]
            api.failure[method] = httpx.Response(502)

    api.before = lose_receipt
    await bot().refresh_status_pin()
    assert "Secret Route" in api.messages[CHAT, 31]

    api.before = lambda method: None
    rt.config = replace(
        config, profiles=(only, replace(other, alerts=AlertSettings(telegram_status_pin=False)))
    )
    await bot().refresh_status_pin()
    assert api.messages[CHAT, 31] == before
    assert api.pins[CHAT] == {20, 31}


@pytest.mark.parametrize("profile_text", ["profiles: !!bool maybe", "profiles: !!int ''"])
async def test_invalid_yaml_tags_keep_saved_profiles_available_on_restart(tmp_path, profile_text):
    running = Rig(tmp_path)
    await running.rt.start()
    saved = running.rt.config
    assert saved is not None
    await running.rt.stop_works_digest()
    running.write(text=profile_text)
    restarted = await running.restart()
    assert restarted.config == saved
    assert "tagged YAML value" in restarted.view().refusal
    await restarted.stop_works_digest()


async def test_reenabling_after_failed_unpin_restores_status_text(rig):
    rt, api, _, bot = rig
    await bot().refresh_status_pin()
    before = api.messages[CHAT, 31]
    config = rt.config
    rt.config = replace(config, profiles=())
    api.failure["unpinChatMessage"] = httpx.Response(502)
    await bot().refresh_status_pin()
    assert api.messages[CHAT, 31] == "Pinned commute status is off."
    assert api.pins[CHAT] == {20, 31}

    rt.config = config
    await bot().refresh_status_pin()
    assert api.messages[CHAT, 31] == before
    assert api.pins[CHAT] == {20, 31}


async def test_first_send_rate_limit_survives_restart(rig):
    rt, api, _, bot = rig
    api.failure["sendMessage"] = error(429, "Too Many Requests", retry_after=180)
    await bot().refresh_status_pin()
    assert api.methods() == ["getChat", "sendMessage"]

    rt.advance(179)
    await bot().refresh_status_pin()
    assert api.methods() == ["getChat", "sendMessage"]
    rt.advance(1)
    await bot().refresh_status_pin()
    assert api.methods()[-3:] == ["getChat", "sendMessage", "pinChatMessage"]
    assert api.pins[CHAT] == {20, 31}


async def test_failed_cooldown_write_retries_without_sending_during_backoff(rig, monkeypatch):
    rt, api, _, bot = rig
    owner = bot()
    api.failure["sendMessage"] = error(429, "Too Many Requests", retry_after=180)

    def write_fails(path, state):
        raise OSError("read-only state directory")

    with monkeypatch.context() as patch:
        patch.setattr(bot_module, "save_state", write_fails)
        await owner.refresh_status_pin()
    rt.advance(30)
    await owner.refresh_status_pin()
    await bot().refresh_status_pin()
    assert api.methods() == ["getChat", "sendMessage"]


async def test_reenabling_after_lost_unpin_receipt_repins_immediately(rig):
    rt, api, _, bot = rig
    owner = bot()
    await owner.refresh_status_pin()
    config = rt.config
    rt.config = replace(config, profiles=())

    def lose_receipt(method):
        if method == "unpinChatMessage":
            api.pins[CHAT].remove(31)
            api.failure[method] = httpx.Response(502)

    api.before = lose_receipt
    rt.advance()
    await owner.refresh_status_pin()
    assert api.pins[CHAT] == {20}

    rt.config = config
    rt.advance()
    await owner.refresh_status_pin()
    assert api.pins[CHAT] == {20, 31}


@pytest.mark.parametrize("reason", ["deleted", "unpinned", "uneditable"])
async def test_recovers_only_its_own_lost_pin(rig, reason):
    rt, api, _, bot = rig
    await bot().refresh_status_pin()
    if reason == "deleted":
        api.pins[CHAT].remove(31)
        del api.messages[CHAT, 31]
    elif reason == "unpinned":
        api.pins[CHAT].remove(31)
    else:
        api.failure["editMessageText"] = error(400, "message can't be edited")
        journey = occ()
        rt.snapshot = view(replace(journey, legs=(replace(journey.legs[0], train_number="42"),)))
    owner = bot()
    await owner.refresh_status_pin()
    rt.advance()
    await owner.refresh_status_pin()
    assert api.pins[CHAT] == {20, 31 if reason == "unpinned" else 32}
    assert api.methods().count("sendMessage") == (1 if reason == "unpinned" else 2)


async def test_rate_limit_and_partial_pin_retry_keep_one_message_across_restart(rig):
    rt, api, path, bot = rig
    owner = bot()
    api.failure["pinChatMessage"] = error(429, "Too Many Requests", retry_after=180)
    await owner.refresh_status_pin()
    assert api.methods().count("sendMessage") == 1
    assert api.pins[CHAT] == {20}
    assert "31" in path.read_text()
    rt.advance(179)
    await owner.refresh_status_pin()
    await bot().refresh_status_pin()
    assert api.methods().count("pinChatMessage") == 1
    rt.advance(1)
    await bot().refresh_status_pin()
    assert api.methods().count("sendMessage") == 1
    assert api.pins[CHAT] == {20, 31}


async def test_state_write_failure_keeps_the_sent_identity_without_pinning_yet(rig, monkeypatch):
    rt, api, _, bot = rig
    owner = bot()

    def write_fails(path, state):
        raise OSError("read-only state directory")

    with monkeypatch.context() as patch:
        patch.setattr(bot_module, "save_state", write_fails)
        await owner.refresh_status_pin()
        rt.advance()
        await owner.refresh_status_pin()
    assert api.methods() == ["getChat", "sendMessage"]
    rt.advance()
    await owner.refresh_status_pin()
    assert api.methods() == ["getChat", "sendMessage", "pinChatMessage"]
    assert api.pins[CHAT] == {20, 31}


@pytest.mark.parametrize("during", ["getChat", "sendMessage"])
async def test_switching_off_during_first_publication_prevents_a_pin(rig, during):
    rt, api, _, bot = rig
    owner = bot()

    def switch_off(method):
        if method == during:
            profile = rt.config.profiles[0]
            disabled = replace(profile, alerts=replace(profile.alerts, telegram_status_pin=False))
            rt.config = replace(rt.config, profiles=(disabled,))

    api.before = switch_off
    await owner.refresh_status_pin()
    assert api.methods() == (["getChat"] if during == "getChat" else ["getChat", "sendMessage"])
    api.calls.clear()
    rt.advance()
    await owner.refresh_status_pin()
    if during == "getChat":
        assert not api.calls
    else:
        assert api.methods() == ["editMessageText", "unpinChatMessage"]
        assert "Office A" not in api.messages[CHAT, 31]
    assert api.pins[CHAT] == {20}


async def test_edit_retries_after_remote_acceptance_without_creating_a_second_pin(rig):
    rt, api, _, bot = rig
    await bot().refresh_status_pin()
    journey = occ()
    rt.snapshot = view(replace(journey, legs=(replace(journey.legs[0], train_number="42"),)))

    def lose_receipt(method):
        if method == "editMessageText" and method not in api.failure:
            api.messages[CHAT, 31] = api.calls[-1][1]["text"]
            api.failure[method] = httpx.Response(502, content=b"gateway error")

    api.before = lose_receipt
    owner = bot()
    await owner.refresh_status_pin()
    assert "train 42" in api.messages[CHAT, 31]
    assert api.methods()[-1] == "editMessageText"
    api.before = lambda method: None
    rt.advance()
    await owner.refresh_status_pin()
    assert api.methods().count("editMessageText") == 2
    await bot().refresh_status_pin()
    assert api.methods().count("editMessageText") == 2
    assert api.methods().count("sendMessage") == 1 and api.pins[CHAT] == {20, 31}


async def test_store_key_order_changes_do_not_edit_the_pin(rig):
    rt, api, _, bot = rig
    first = rt.config.profiles[0]
    rt.config = replace(rt.config, profiles=(first, replace(first, id="p2")))
    day = WorksDay(rt.now().date(), None)
    rt.snapshot = replace(
        rt.snapshot,
        works={"p2|outbound": (day,), "p1|outbound": (day,)},
        lookahead_last_ok={"p2|outbound": None, "p1|outbound": rt.now() - timedelta(days=2)},
    )
    await bot().refresh_status_pin()
    rt.snapshot = replace(
        rt.snapshot,
        works=dict(reversed(tuple(rt.snapshot.works.items()))),
        lookahead_last_ok=dict(reversed(tuple(rt.snapshot.lookahead_last_ok.items()))),
    )
    await bot().refresh_status_pin()
    assert "editMessageText" not in api.methods()


@pytest.mark.parametrize("key", ["live_today", "telegram_status_pin"])
@pytest.mark.parametrize("setting", [None, "true", "false", '"false"', "null"])
def test_status_preferences_validate_and_load_legacy_values(key, setting):
    text = ONE_PROFILE
    if setting is not None:
        text = text.replace("    name:", f"    alerts: {{{key}: {setting}}}\n    name:", 1)
    profiles, errors = parse_profiles(text, None)
    if setting in ('"false"', "null"):
        assert [error.path for error in errors] == [f"profiles[0].alerts.{key}"]
    else:
        assert not errors and getattr(profiles[0].alerts, key) is (setting != "false")
        assert decode(AlertSettings, encode(profiles[0].alerts)) == profiles[0].alerts
    old = encode(AlertSettings())
    old.pop(key)
    assert getattr(decode(AlertSettings, old), key) is True

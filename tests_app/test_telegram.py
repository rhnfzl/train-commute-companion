import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from builders import AMS
from conftest import CHAT, TOKEN, FakeTelegram, error
from tcc_app.telegram import (
    TelegramApi,
    TelegramError,
    TelegramNotifier,
    callback_data,
    split_text,
)
from test_runtime import DAY, FakeClock, Rig, at

from train_commute_companion.delivery import Obligation
from train_commute_companion.model import AlertEvent, Direction, EventKind, Severity

TEXT = "Office A: your UT to RTD train is cancelled. Next good connection: 08:44, platform 5."


def event(severity: Severity = Severity.LOUD, key: str = "live|p1|outbound|2026-10-27|c",
          text: str = TEXT) -> AlertEvent:  # fmt: skip
    return AlertEvent("p1", "office_a", Direction.OUTBOUND, DAY, 0, EventKind.CANCELLED,
                      severity, None, text, key, at("07:50"))  # fmt: skip


def app_event(kind: EventKind, key: str, created: datetime) -> AlertEvent:
    return AlertEvent("", "app", None, None, None, kind, Severity.LOUD, None, f"App: {key}",
                      key, created)  # fmt: skip


def notifier(tg: FakeTelegram, clock: FakeClock) -> TelegramNotifier:
    return TelegramNotifier(TelegramApi(TOKEN, tg.client()), CHAT, clock.now)


async def sent_markup(tg: FakeTelegram, clock: FakeClock, e: AlertEvent) -> dict | None:
    assert await notifier(tg, clock).send(e)
    return tg.sent[-1].get("reply_markup")


async def test_loud_and_silent_map_to_disable_notification(tg, clock):
    n = notifier(tg, clock)
    assert await n.send(event(Severity.LOUD)) and await n.send(event(Severity.SILENT))
    assert [m["disable_notification"] for m in tg.sent] == [False, True]
    assert tg.sent[0]["text"] == event(Severity.LOUD).text and tg.sent[0]["chat_id"] == CHAT
    assert all("parse_mode" not in m for m in tg.sent)


async def test_journey_alert_has_leaving_and_not_travelling_buttons(tg, clock):
    row = (await sent_markup(tg, clock, event()))["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["I'm leaving", "Not travelling"]
    assert row[0]["callback_data"] == "l|p1|outbound|2026-10-27"
    assert row[1]["callback_data"] == "s|p1|outbound|2026-10-27"


async def test_application_warning_has_no_buttons(tg, clock):
    assert await sent_markup(tg, clock, app_event(EventKind.KEY_REJECTED, "k", at("07:50"))) is None


async def test_lookahead_message_has_no_buttons(tg, clock):
    e = replace(event(), kind=EventKind.WORKS_AHEAD, key="lookahead|p1|outbound|2026-10-28|w")
    assert await sent_markup(tg, clock, e) is None


async def test_too_long_callback_data_leaves_the_buttons_out(tg, clock):
    assert await sent_markup(tg, clock, replace(event(), profile_id="p" * 60)) is None


def test_callback_data_too_long_or_with_bar_is_none():
    assert callback_data("l", "x" * 70, "outbound", "2026-10-27") is None
    assert callback_data("l", "a|b", "outbound", "2026-10-27") is None
    assert callback_data("l", "p1", "outbound", "2026-10-27") == "l|p1|outbound|2026-10-27"


def test_split_text_prefers_newline_then_space_then_hard():
    assert split_text("short") == ["short"]
    assert split_text("aaaa\nbb cc", 7) == ["aaaa", "bb cc"]
    assert split_text("aa bb cc", 5) == ["aa bb", "cc"]
    assert split_text("x" * 12, 5) == ["xxxxx", "xxxxx", "xx"]
    assert all(len(p) <= 4096 for p in split_text(("word " * 3000).strip()))


FAILURES = {
    "network": httpx.ConnectError("boom"),
    403: error(403, "Forbidden: bot was blocked by the user"),
    500: error(500, "Internal Server Error"),
    429: error(429, "Too Many Requests: retry after 7", retry_after=7),
    400: error(400, "Bad Request: BUTTON_DATA_INVALID"),
    "chat not found": error(400, "Bad Request: chat not found"),
}


@pytest.mark.parametrize(
    "failure, wait_s",
    [("network", 60), (403, 60), (500, 60), (429, 7), (400, 60), ("chat not found", 60)],
)
async def test_failure_is_not_delivered_and_backs_off(tg, clock, failure, wait_s):
    n = notifier(tg, clock)
    tg.fail = lambda m: FAILURES[failure]
    assert not await n.send(event())
    asked = len(tg.calls)
    assert asked == (2 if failure == 400 else 1)  # a plain 400 is tried again without buttons
    await clock.sleep(wait_s - 1)
    assert not await n.send(event())
    if failure == "chat not found":  # global: another event waits too
        assert not await n.send(event(key="other"))
    assert len(tg.calls) == asked
    await clock.sleep(1)
    tg.fail = lambda m: None
    assert await n.send(event())


async def test_backoff_holds_across_the_autumn_repeat_hour(tg):
    t = [datetime(2026, 10, 25, 2, 59, 30, tzinfo=AMS)]
    assert t[0].astimezone(UTC).hour == 0
    n = TelegramNotifier(TelegramApi(TOKEN, tg.client()), CHAT, lambda: t[0])
    tg.fail = lambda m: httpx.ConnectError("boom")
    assert not await n.send(event())
    t[0] = datetime(2026, 10, 25, 2, 0, 40, fold=1, tzinfo=AMS)  # 70 s later
    assert t[0].astimezone(UTC) == datetime(2026, 10, 25, 1, 0, 40, tzinfo=UTC)
    tg.fail = lambda m: None
    assert await n.send(event())


async def test_bad_request_for_one_message_does_not_block_the_next(tg, clock):
    n = notifier(tg, clock)
    tg.fail = lambda m: error(400, "Bad Request: BUTTON_DATA_INVALID")
    assert not await n.send(event(key="a"))
    assert tg.sent == [] and len(tg.calls) == 2
    tg.fail = lambda m: None
    assert await n.send(event(key="b"))
    assert not await n.send(event(key="a"))  # still waits out its own minute
    assert len(tg.sent) == 1


async def test_bad_buttons_are_dropped_and_the_text_still_goes(tg, clock):
    n = notifier(tg, clock)
    tg.fail = lambda m: (
        error(400, "Bad Request: BUTTON_DATA_INVALID") if len(tg.calls) == 1 else None
    )
    assert await n.send(event())
    assert tg.sent[0]["text"] == TEXT and "reply_markup" not in tg.sent[0]


def fail_third_part(tg: FakeTelegram, failing: list[bool]) -> None:
    tg.fail = lambda m: error(500, "Internal Server Error") if failing[0] and len(tg.sent) >= 2 \
        else None  # fmt: skip


async def test_long_text_is_split_and_resumes_at_the_failed_part(tg, clock):
    n, failing = notifier(tg, clock), [True]
    fail_third_part(tg, failing)
    long = event(text="x" * 9000)
    assert not await n.send(long)
    await clock.sleep(60)
    assert not await n.send(long)
    assert tg.calls.count("sendMessage") == 4
    await clock.sleep(60)
    failing[0] = False
    assert await n.send(long)
    assert [len(m["text"]) for m in tg.sent] == [4096, 4096, 808]
    assert "".join(m["text"] for m in tg.sent) == long.text
    assert [("reply_markup" in m) for m in tg.sent] == [False, False, True]


async def test_changed_payload_under_the_same_key_is_sent_in_full(tg, clock):
    n, failing = notifier(tg, clock), [True]
    fail_third_part(tg, failing)
    assert not await n.send(event(text="x" * 9000))
    renamed = event(text="y" * 100)  # a rename shortened it under the same key
    tg.fail = lambda m: error(500, "Internal Server Error")
    await clock.sleep(60)
    assert not await n.send(renamed)
    tg.fail = lambda m: None
    await clock.sleep(60)
    assert await n.send(renamed)
    assert tg.sent[-1]["text"] == renamed.text and len(tg.sent) == 3


async def test_no_chat_id_is_never_delivered(tg, clock):
    n = TelegramNotifier(TelegramApi(TOKEN, tg.client()), None, clock.now)
    assert not await n.send(event())
    assert tg.calls == []


def held(kind: EventKind, key: str, created: datetime) -> Obligation:
    return Obligation(key, app_event(kind, key, created), None, None, 1, None, key, None, 0)


async def test_failing_telegram_backs_off_and_delivers_later(tmp_path, tg):  # Review Focus 1
    rig = Rig(tmp_path)
    rig.notifier = TelegramNotifier(TelegramApi(TOKEN, tg.client()), CHAT, rig.clock.now)
    rig.rt = rig.new()
    tg.fail = lambda m: httpx.ConnectError("boom")
    raised = {"07:51": EventKind.KEY_REJECTED, "08:05": EventKind.PROFILE_REFUSED,
              "08:20": EventKind.STORAGE_FAILED}  # fmt: skip
    while rig.clock.t < at("08:50"):
        hhmm = rig.clock.t.strftime("%H:%M")
        if hhmm in raised and rig.clock.t.second == 0:
            o = held(raised[hhmm], f"a{hhmm}", rig.clock.now())
            rig.rt._st = replace(rig.rt._st, obligations=(*rig.rt._st.obligations, o))
        await rig.rt.watchdog_once()
        await rig.clock.sleep(30)
    assert tg.calls.count("sendMessage") <= 61  # one per 60 s window, not one per alert per tick
    assert rig.rt.view().undelivered > 0
    tg.fail = lambda m: None
    await rig.rt.watchdog_once()
    assert [m["text"] for m in tg.sent] == ["App: a07:51", "App: a08:05", "App: a08:20"]
    assert rig.rt.view().undelivered == 0


async def test_raising_sender_does_not_block_later_alerts(tmp_path):  # plan-1 DEFER 11
    rig = Rig(tmp_path)

    class Raising:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, e: AlertEvent) -> bool:
            if e.key == "a":
                raise RuntimeError("shell bug")
            self.sent.append(e.key)
            return True

    rig.notifier = raising = Raising()  # type: ignore[assignment]
    rig.rt = rig.new()
    now = rig.clock.now()
    a, b = held(EventKind.KEY_REJECTED, "a", now), held(EventKind.PROFILE_REFUSED, "b", now)
    rig.rt._st = replace(rig.rt._st, obligations=(a, b))
    await rig.rt.watchdog_once()
    assert raising.sent == ["b"]
    assert [o.id for o in rig.rt._st.obligations] == ["a"]


async def test_token_never_in_errors_or_logs(tg, caplog):  # Review Focus 4
    api = TelegramApi(TOKEN, tg.client())
    caplog.set_level(logging.DEBUG)  # root only: the transport loggers keep their WARNING
    raised: list[TelegramError] = []
    for failure in (httpx.ConnectError(f"https://api.telegram.org/bot{TOKEN}/x"),
                    httpx.Response(500, text="<html>oops</html>"), None):  # fmt: skip
        tg.fail = lambda m, f=failure: f
        try:
            await api.call("sendMessage", {"chat_id": CHAT, "text": "hi"})
        except TelegramError as e:
            raised.append(e)
    assert [e.code for e in raised] == [0, 0] and len(tg.sent) == 1
    assert api.bot_id == "123"
    assert TOKEN not in caplog.text and all(TOKEN not in str(e) for e in raised)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    assert all(e.__cause__ is None and e.__context__ is None for e in raised)


def test_telegram_error_carries_code_and_retry_after():
    e = TelegramError(429, 7, "Too Many Requests")
    assert (e.code, e.retry_after, str(e)) == (429, 7, "Too Many Requests")

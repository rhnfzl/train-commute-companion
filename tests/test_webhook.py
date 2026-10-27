import base64
import hmac
import json
import re
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from builders import AMS as TZ
from webhook_receiver import Receiver, doc_verify

from train_commute_companion.model import AlertEvent, Direction, EventKind, Severity, TrainId
from train_commute_companion.notify import event_fields
from train_commute_companion.webhook import (
    PROBE_LOUD,
    PROBE_SILENT,
    WebhookTestResult,
    body,
    check_secret,
    check_token,
    event_id,
    headers,
    parse_urls,
    probe_body,
    probe_id,
    signature,
    url_id,
)

SECRET = "test-webhook-secret-0001"
URL1, URL2 = "https://hooks.example.invalid/tcc", "https://hooks2.example.invalid/tcc"
DOCS = Path(__file__).resolve().parent.parent / "docs" / "webhooks.md"
DAY = date(2026, 10, 27)
TRAIN = TrainId(DAY, "1234", datetime(2026, 10, 27, 8, 12, tzinfo=TZ))
CREATED = datetime(2026, 10, 27, 7, 30, tzinfo=TZ)
alert = AlertEvent(
    "office_a",
    "office_a",
    Direction.OUTBOUND,
    DAY,
    0,
    EventKind.CANCELLED,
    Severity.LOUD,
    TRAIN,
    "Office A: your UT to RTD train is cancelled. Next good connection: 08:44, platform 5.",
    "cancelled:2026-10-27:1234",
    CREATED,
)
quiet_alert = AlertEvent(
    "office_a",
    "office_a",
    None,
    None,
    None,
    EventKind.PLATFORM_CHANGE,
    Severity.SILENT,
    None,
    "Office A: platform change.",
    "platform",
    CREATED,
)
# the obligation id from the brief, built so the time zone offset is not a literal
OB = f"p1|outbound|cancelled:2026-10-27:1234@{TRAIN.planned_departure.isoformat()}:x|loud|{CREATED.isoformat()}"


def test_signature_matches_standard_webhooks():
    b = b'{"a":1}'
    want = base64.b64encode(
        hmac.new(SECRET.encode(), b"tcc_1.1700000000." + b, "sha256").digest()
    ).decode()
    assert signature(SECRET, "tcc_1", 1700000000, b) == f"v1,{want}"
    raw = b"0123456789abcdef0123"
    wh = "whsec_" + base64.b64encode(raw).decode()
    mac = hmac.new(raw, b"tcc_1.1700000000." + b, "sha256").digest()
    assert signature(wh, "tcc_1", 1700000000, b) == "v1," + base64.b64encode(mac).decode()


def test_headers_carry_idempotency_key_and_the_token_only_when_set():
    h = headers(SECRET, "tcc_1", 1700000000, b"{}")
    assert h["idempotency-key"] == "tcc_1" and "authorization" not in h
    assert h["content-type"] == "application/json" and h["webhook-timestamp"] == "1700000000"
    assert h["webhook-id"] == "tcc_1" and h["webhook-signature"] == signature(
        SECRET, "tcc_1", 1700000000, b"{}"
    )
    with_token = headers(SECRET, "tcc_1", 1700000000, b"{}", token="test-webhook-token-0001")
    assert with_token["authorization"] == "Bearer test-webhook-token-0001"


def test_event_fields_are_the_ha_bus_event_fields():
    assert event_fields(alert) == {
        "profile_id": "office_a",
        "profile_name": "office_a",
        "direction": "outbound",
        "service_date": "2026-10-27",
        "leg_index": 0,
        "kind": "cancelled",
        "severity": "loud",
        "train_operating_date": "2026-10-27",
        "train_number": "1234",
        "train_departure": TRAIN.planned_departure.isoformat(),
        "text": alert.text,
        "key": alert.key,
        "created_at": CREATED.isoformat(),
    }


def test_body_is_the_ha_fields_plus_version_attribution_and_test():
    got = json.loads(body(alert))
    assert got == {"version": 1, "attribution": "Data: NS", "test": False, **event_fields(alert)}
    assert got["severity"] == "loud" and got["train_number"] == alert.train.train_number  # type: ignore[union-attr]
    quiet = json.loads(body(quiet_alert))
    assert quiet["train_number"] is None and quiet["leg_index"] is None
    assert quiet["severity"] == "silent" and quiet["direction"] is None
    assert b'": ' not in body(alert) and b', "' not in body(alert)  # compact separators


def test_probe_body_is_marked_and_shaped_like_an_alert():  # owner decision Q2(b)
    loud, silent = (
        json.loads(probe_body(Severity.LOUD, CREATED)),
        json.loads(probe_body(Severity.SILENT, CREATED)),
    )
    assert list(loud) == list(json.loads(body(alert))) and loud["test"] is True
    assert loud["kind"] == "test" and loud["created_at"] == CREATED.isoformat()
    assert (loud["severity"], loud["text"], silent["severity"], silent["text"]) == (
        "loud",
        PROBE_LOUD,
        "silent",
        PROBE_SILENT,
    )
    assert loud["profile_id"] is None and loud["train_number"] is None
    assert loud["version"] == 1 and loud["attribution"] == "Data: NS"
    assert probe_id(Severity.LOUD, CREATED) != probe_id(Severity.SILENT, CREATED)
    assert probe_id(Severity.LOUD, CREATED) != event_id(OB)
    assert probe_id(Severity.LOUD, CREATED) == probe_id(Severity.LOUD, CREATED)


def test_webhook_test_result_ok_and_summary():
    assert WebhookTestResult("hooks.example.invalid", 200, 204).ok
    assert not WebhookTestResult("hooks.example.invalid", 200, 503).ok
    assert not WebhookTestResult("h", "TimeoutError", 200).ok
    assert (
        WebhookTestResult("hooks2.example.invalid", 401, "ConnectError").summary()
        == "hooks2.example.invalid (loud 401, silent ConnectError)"
    )


def test_event_id_is_a_short_stable_hash_of_the_obligation_id():
    assert event_id(OB) == event_id(OB) and re.fullmatch(r"tcc_[0-9a-f]{32}", event_id(OB))
    assert event_id(OB) != event_id(OB.replace("p1|", "p2|", 1))
    assert event_id(OB) != event_id(OB.replace("|outbound|", "|return|", 1))
    assert event_id(OB) != event_id(OB.replace("|loud|", "|silent|", 1))


@pytest.mark.parametrize(
    "text,want",
    [
        (URL1, (URL1,)),
        (f" {URL1}, {URL2}\n{URL1} ", (URL1, URL2)),
        ("", ()),
        (" ,\n", ()),
    ],
)
def test_parse_urls(text, want):
    assert parse_urls(text) == want


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://hooks.example.invalid/x",
        "https://user:pw@hooks.example.invalid/x",
        "hooks.example.invalid",
        "https://",
        "https://hooks.example.invalid:abc/x",
        "https://hooks.example.invalid:99999/x",
        "https://hooks.example.invalid:0/x",
        "https://[::zz]/x",
        "https://hooks.example.invalid/a\x00b",
    ],
)
def test_parse_urls_refuses_naming_the_entry_not_the_value(bad):
    with pytest.raises(ValueError, match="entry 2") as e:
        parse_urls(f"{URL1} {bad}")
    assert bad not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


@pytest.mark.parametrize(
    "bad",
    [
        "x" * 15,  # short text
        "whsec_" + "!" * 12,  # 16+ characters, not strict base64
        "whsec_" + "A" * 13,
        "whsec_" + base64.b64encode(b"x" * 12).decode(),  # 22 characters, a 12-byte key
        "whsec_" + "é" * 24,  # non-ASCII: b64decode raises plain ValueError
    ],
)
def test_check_secret_refuses_short_text_bad_base64_and_short_keys(bad):
    with pytest.raises(ValueError) as e:
        check_secret(bad)
    assert bad not in str(e.value)


def test_check_secret_accepts_text_and_prefixed_keys():
    check_secret("x" * 16)
    check_secret(SECRET)
    check_secret("whsec_" + base64.b64encode(bytes(range(7, 23))).decode())


@pytest.mark.parametrize(
    "bad", ["x" * 15, "tést-webhook-token-01", "test-webhook token-01", "test-webhook-token\n01"]
)
def test_check_token_refuses_short_non_ascii_and_spaced(bad):
    with pytest.raises(ValueError) as e:
        check_token(bad)
    assert bad not in str(e.value)


def test_check_token_accepts_a_header_safe_token():
    check_token("test-webhook-token-0001")


def test_url_id_is_short_stable_and_hides_the_url():
    assert url_id(URL1) == url_id(URL1) and re.fullmatch(r"[0-9a-f]{16}", url_id(URL1))
    assert url_id(URL1) != url_id(URL2) and "tcc" not in url_id(URL1)


def test_documented_verify_accepts_ours_and_rejects_tampering_and_old_timestamps():
    verify = doc_verify()
    b = body(alert)
    h = headers(SECRET, event_id(OB), 1700000000, b)
    assert verify(SECRET, h, b, now=1700000100)
    assert not verify(SECRET, h, b[:-1] + b" ", now=1700000100)
    assert not verify("another-secret-00001", h, b, now=1700000100)
    assert not verify(SECRET, h, b, now=1700000000 + 301)
    wh = "whsec_" + base64.b64encode(bytes(range(16))).decode()
    assert verify(wh, headers(wh, event_id(OB), 1700000000, b), b, now=1700000100)


@pytest.mark.parametrize("now", [1700000100, 1700000100.5])
@pytest.mark.parametrize(
    "damage",
    [
        lambda h: {k: v for k, v in h.items() if k != "webhook-id"},
        lambda h: {k: v for k, v in h.items() if k != "webhook-signature"},
        lambda h: {**h, "webhook-timestamp": "soon"},
        lambda h: {**h, "webhook-timestamp": "9" * 400},
        lambda h: {**h, "webhook-timestamp": "-" + "9" * 400},
        lambda h: {**h, "webhook-timestamp": "²"},
        lambda h: {**h, "webhook-signature": "v1,éé"},
    ],
)
def test_documented_verify_returns_false_on_malformed_requests(damage, now):
    verify = doc_verify()
    b = body(alert)
    h = headers(SECRET, event_id(OB), 1700000000, b)
    assert verify(SECRET, damage(h), b, now=now) is False
    for secret in ("whsec_" + "!" * 12, "whsec_" + "é" * 24):
        assert verify(secret, damage(h), b, now=now) is False
        assert verify(secret, h, b, now=now) is False


def test_receiver_keeps_verified_posts_and_counts_bad_ones():
    rx = Receiver(SECRET, lambda: 1700000100.0, status=204)
    b = body(alert)
    with httpx.Client(transport=rx.transport()) as c:
        good = c.post(URL1, content=b, headers=headers(SECRET, event_id(OB), 1700000000, b))
        c.post(URL1, content=b, headers=headers("another-secret-00001", "tcc_1", 1700000000, b))
    assert good.status_code == 204 and rx.posts == [json.loads(b)] and rx.bad == 1
    assert len(rx.requests) == 2
    rx.now = None
    with pytest.raises(AssertionError):
        rx.handler(httpx.Request("POST", URL1, content=b))


def _section(md: str, heading: str) -> str:
    return md.split(heading, 1)[1].split("\n## ", 1)[0]


def test_docs_warn_about_payload_contents():  # owner decision Q2(a)
    sec = _section(DOCS.read_text(encoding="utf-8"), "## Security and privacy").lower()
    for words in ("profile names", "stations", "times", "this machine", "https"):
        assert words in sec, words

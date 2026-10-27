# Webhooks

The companion can post each alert as signed JSON to URLs you choose, for example an AI assistant such as Hermes Agent or OpenClaw, or your own script. Webhooks are outbound only: the companion never listens for them. They work the same from Docker and from Home Assistant.

Data: NS.

## What is sent

One POST per alert to every configured URL. The webhook gets the same alerts as your phone, with the same text, and quiet hours hold silent alerts back for both. The phone stays the channel that counts: a webhook that is down never holds back or repeats a phone alert.

## Headers

The signature follows [Standard Webhooks](https://www.standardwebhooks.com/).

| Header | Value |
|---|---|
| `content-type` | `application/json` |
| `webhook-id` | The alert's id, for example `tcc_` and 32 hex characters. The same for every retry of one alert. |
| `webhook-timestamp` | When this request was signed, in whole Unix seconds. |
| `webhook-signature` | `v1,` and the base64 HMAC-SHA256 of `<webhook-id>.<webhook-timestamp>.<raw body>`, keyed with your secret. |
| `idempotency-key` | The same value as `webhook-id`. |
| `authorization` | `Bearer <token>`, only when you set a bearer token. |

A secret that starts with `whsec_` is used as Standard Webhooks writes it: the rest is base64 and the decoded bytes are the key. Any other secret is used as its UTF-8 text.

## Payload

The body is one JSON object, UTF-8, the same fields Home Assistant puts on its alert event plus `version`, `attribution` and `test`.

| Field | Type | Meaning |
|---|---|---|
| `version` | integer | The payload schema version, now `1`. |
| `attribution` | string | Always `"Data: NS"`. Show it with the text. |
| `test` | boolean | `false` for an alert. `true` for a setup test send: answer it with a 2xx and do not act on it. |
| `profile_id` | string or null | The profile id, such as `office_a`. Null in a test send. |
| `profile_name` | string or null | The profile name. |
| `direction` | string or null | `outbound` or `return`, null when the alert is not about one direction. |
| `service_date` | string or null | The travel day, `YYYY-MM-DD`. |
| `leg_index` | integer or null | Which leg of the journey, from 0; null when the alert is not about one leg. |
| `kind` | string | What happened, such as `cancelled`, `transfer_at_risk` or `leave_now`; `test` in a test send. |
| `severity` | string | `loud` (must not be missed) or `silent`. |
| `train_operating_date` | string or null | The train's operating day, `YYYY-MM-DD`. |
| `train_number` | string or null | The train number. |
| `train_departure` | string or null | The train's planned departure, ISO 8601 with offset. |
| `text` | string | The alert as your phone shows it. Use this text as it is. |
| `key` | string or null | The fact the alert is about. Several alerts can share a key; use `webhook-id` to dedupe. |
| `created_at` | string | When the alert was made, ISO 8601 with offset. |

An example (times carry their offset; this one shows them in UTC):

```json
{
  "version": 1,
  "attribution": "Data: NS",
  "test": false,
  "profile_id": "office_a",
  "profile_name": "office_a",
  "direction": "outbound",
  "service_date": "2026-10-27",
  "leg_index": 0,
  "kind": "cancelled",
  "severity": "loud",
  "train_operating_date": "2026-10-27",
  "train_number": "1234",
  "train_departure": "2026-10-27T07:12:00+00:00",
  "text": "Office A: your UT to RTD train is cancelled. Next good connection: 08:44, platform 5.",
  "key": "cancelled:2026-10-27:1234",
  "created_at": "2026-10-27T06:30:00+00:00"
}
```

## Verify a delivery

Check every request before you act on it. This function needs only the Python standard library:

```python
import base64, hashlib, hmac, time


def verify(secret, headers, body, now=None, tolerance=300):
    """True when the request is from your companion and fresh; False for anything else,
    malformed requests included. `body` is the raw bytes."""
    try:
        msg_id, ts = headers["webhook-id"], headers["webhook-timestamp"]
        sigs = headers["webhook-signature"].split()
        if not (ts.isascii() and ts.isdigit() and len(ts) <= 12):
            return False  # bounded before any arithmetic
        if abs((time.time() if now is None else now) - int(ts)) > tolerance:
            return False
        if secret.startswith("whsec_"):
            key = base64.b64decode(secret[6:], validate=True)
        else:
            key = secret.encode()
    except (KeyError, AttributeError, ValueError):  # ValueError covers binascii.Error
        return False
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    want = ("v1," + base64.b64encode(mac).decode()).encode()
    return any(hmac.compare_digest(want, s.encode()) for s in sigs)
```

Pass the header names in lower case and the body exactly as received, before any JSON parsing.

Delivery is at least once: a retry after a lost answer sends the same alert again with the same `webhook-id`. Remember the ids you handled and skip a repeat. `idempotency-key` carries the same value for receivers that dedupe on it.

### Hermes Agent

Add a webhook route in Hermes, for example `http://localhost:8644/webhooks/commute`, with the same secret as the companion. Hermes checks the Standard Webhooks headers itself and dedupes on `webhook-id`.

### OpenClaw

OpenClaw checks no signature. Set the companion's bearer token setting to your OpenClaw hook token, and point the URL at a mapped hook whose template turns the payload's `text` into the agent `message`. OpenClaw refuses a token in the URL query, so use the bearer token. This setup has not been checked against a live OpenClaw.

## Delivery

Your phone is the channel that counts: an alert is delivered once the phone has it, and a webhook never counts as delivered. A webhook that is down never holds back or repeats a phone alert.

- A copy of an alert that a receiver did not take (any answer other than a 2xx, a redirect, an error or no answer within 5 seconds) is kept, survives a restart, and is tried again on later delivery passes after a 60 second rest.
- Before each try the copy is checked again, like a phone message: expired advice, a journey you said you are not travelling or a message a newer one covers is not sent, and a renamed profile shows its new name.
- A look-ahead message is sent with only the facts that still hold at send time, so it never names a leg that is back to normal again or works that are over.
- A copy is given up 2 hours after its first try, also while it waits for fresh NS data after a restart.
- A receiver that stays down gets at most one try per 60 seconds. Its tries go to the owed copies in turn, so after an outage alerts can arrive out of order: order them by `created_at`. When alerts come faster than one a minute, at most 200 copies are kept: the oldest copy never tried is dropped first, else the oldest copy, with one warning in the log each.
- The log shows one warning per outage, naming the receiver's host only, and one line when it works again.
- A restart, or a timeout after the receiver accepted, can send an alert again with the same `webhook-id`.
- Turning webhooks off (no URLs) drops the copies still owed; they are not sent when you turn webhooks on again.
- In Docker, `webhooks_failing` means "the last try to some webhook failed". It clears only on the next success, and it shows only in the full `/health` report, with `TCC_HEALTH_TOKEN`.

## Test sends

When you set up webhooks, each URL gets a signed loud and a signed silent test send. They have `"test": true` and their own `webhook-id`s. Home Assistant sends them when you save a changed webhook setting; Docker sends them on the Telegram `/test` command, after the phone test.

Your receiver must answer both with a 2xx. If it does not, Home Assistant refuses to save the setting and shows the host with both results, and Telegram reports the URL as not accepted. Test sends are never retried or stored. Your phone, with your confirmation, stays the proof that alerts arrive.

## Security and privacy

The payload carries your profile names, stations and times, and a receiver may be a third-party AI service. Use a receiver on this machine where you can, and https for any receiver off it.

- Generate the secret with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Redirects are never followed: a receiver that answers with a redirect counts as failed.
- The bearer token goes to every configured URL, so use it only with receivers you run.

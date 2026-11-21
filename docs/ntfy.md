# Phone push with ntfy

Get train alerts on your phone without Home Assistant or a Telegram bot.
You need the ntfy phone app, your own NS API key, and your own ntfy destination.
The Docker app sends one-way notifications. Your app stays on your home network.

## Configure your destination

1. Download the files in [Docker first run](docker.md#first-run).
2. Install the ntfy app on your phone and allow notifications.
3. Create your own ntfy account and access token on your chosen server.
4. Generate a private random topic, for example with `openssl rand -hex 24`.
5. Protect that topic against anonymous reads and writes on the server.
6. Subscribe to the exact server and topic in the phone app with your account credentials.
7. Set your `NS_API_KEY`, `TCC_NTFY_TOPIC`, and `TCC_NTFY_TOKEN` in the private `.env` file.
8. Leave `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` blank for ntfy-only mode.
9. Set your stations and travel times in `config/profiles.yaml`.
10. Start with `docker compose up -d` and read the private setup log.

The default server is `https://ntfy.sh`. For your own server, set `TCC_NTFY_SERVER` to its HTTPS root.
The URL cannot contain a path, credentials, query, or fragment. Redirects are refused without forwarding credentials.
The topic must contain 32-64 letters, digits, `_` or `-`. Use random text, never station names or personal details.
The access token must contain at least 16 printable ASCII characters without spaces.
Use your own account and token. The project provides no shared account, key, or topic.

A random topic is not access control. An access token alone does not make a public topic private.
Reserve a protected topic with your provider, or configure your own server to deny anonymous access.
Confirm those permissions before sending route information. Providers can require a paid plan for protected topics.
See ntfy's [access control](https://docs.ntfy.sh/config/#access-control) and [token authentication](https://docs.ntfy.sh/publish/#access-tokens).

Partial settings fail startup, even when Telegram is present. A server or token alone never enables sending.
Configured ntfy is enabled by default and becomes the primary phone destination.
`TCC_NTFY_ENABLED=false` disables it; valid Telegram configuration is then required.
Apply environment changes with `docker compose up -d --force-recreate tcc`.

Each profile also has a default-on switch. Home Assistant exposes it as **ntfy phone alerts**
in the profile details form. The same setting is available in YAML:

```yaml
alerts:
  ntfy_enabled: false
```

Set it to `false` to stop that profile's ntfy alerts, including pending sends.
Email and webhook copies keep their existing settings. Telegram-only and Home Assistant phone alerts are unaffected.
Turning it back on applies to new alerts, without adding recipients to old queued messages.
The saved opt-out survives quiet hours, retries and restarts, including timetable and planned-work messages.
The same disruption revision stays off for recipients who opted out.
A new revision can reach them after they turn alerts back on.
For a long alert, the app checks the switch again before sending each part.
A request already sent to the push server can still deliver.
App-wide reliability warnings and explicit delivery tests do not belong to a profile and still use the configured transport.

## Confirm both phone alerts

The existing generated or explicit `TCC_HEALTH_TOKEN` protects the local setup endpoints.
Read the generated token from the first startup log and keep it private, as described in [Docker setup](docker.md#first-run).
The token also grants full health and Today access to every profile.

Run these commands on your Docker host. Set the shell variable `TCC_HEALTH_TOKEN` to your local health token first.
Keep the app on a trusted home network. Use HTTPS if authentication crosses an untrusted network.
Never put tokens in URLs or forward port 8080 to the internet.

Send one loud and one quiet test:

```bash
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $TCC_HEALTH_TOKEN" \
  http://127.0.0.1:8080/delivery/test
```

HTTP acceptance only means the push server accepted the messages. It does not confirm phone delivery.
Read both notifications on your phone. Check the loud test's sound and the quiet test's behavior.
Quiet notifications may appear only in notification history or the app.
Each message contains a different confirmation code. Enter both locally within 15 minutes:

```bash
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $TCC_HEALTH_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"loud":"CODE_FROM_LOUD_TEST","quiet":"CODE_FROM_QUIET_TEST"}' \
  http://127.0.0.1:8080/delivery/confirm
```

The response says `delivery_confirmed: true` only after both fresh codes match and confirmation is saved.
Do not confirm alerts you did not receive. These endpoints have no journey actions.
They require Bearer authentication on POST requests and are absent when ntfy is disabled.
Browser Basic credentials for Today cannot trigger setup sends or confirmation.
Neither endpoint returns the phone codes. No incoming ntfy callbacks or public app URL are needed.

`/health` stays 503 until phone confirmation and all other required health checks pass.
Confirmation persists privately in `/data/ntfy-proof.json`, bound to the server, topic, and token.
Changing any of those requires a new test. Another chat or topic's confirmation never transfers.
Restarting or requesting another test invalidates unconfirmed codes. Failed pairs cannot confirm delivery.
If saving confirmation fails, fix storage and retry confirmation before the codes expire.

## Alerts and actions

Loud alerts request ntfy high priority (`4`). Quiet alerts request minimum priority (`1`), without sound or vibration.
Your phone, ntfy subscription, operating system, Focus, battery controls, and network determine the actual result.
This feature does not provide critical alerts or guaranteed wake-up behavior.
See ntfy's [priority behavior](https://docs.ntfy.sh/publish/#message-priority).

Existing profile notification categories, quiet hours, pauses, and `primary` channel routing apply to ntfy.
Email and signed webhook copies remain optional. Missing optional transports do not block ntfy.
Failures keep the existing durable delivery obligation for retry, with a 60-second transport backoff.
An accepted message can repeat after a crash before its receipt is saved. Delivery remains at least once.

ntfy has no answer buttons, location sharing, or journey actions in this integration.
Use existing Telegram or Home Assistant surfaces where configured for answers, leaving, skipping, and recovery actions.
Without those surfaces, ntfy-only mode still follows configured journeys and sends alerts.
You cannot answer a station question or change a journey through ntfy or Today.
MCP can read journey status in ntfy-only mode. The travel plan tools require Telegram.

You may retain Telegram for commands and answers alongside ntfy primary alerts.
Telegram still uses its own private claim and `/test` confirmation for its actions.
Telegram confirmation does not confirm ntfy delivery, and Telegram `/test` does not run the ntfy test.
Existing Telegram-only setups retain their delivery and setup behavior.

## Privacy and iOS limits

Your configured push service receives the alert text and topic.
Text can contain profile names, stations, train numbers, route changes, and travel or departure times.
Setup messages also contain temporary phone confirmation codes.
The service receives your account token in the authorization header and can store or process messages under its own policy.
Use a trusted service or your own server. Protect the account, topic, `.env`, phone subscription, and local setup token.
Topic, token, and authorization headers are excluded from application errors, state exports, health reports, and logs.
Only a credential-bound destination hash is persisted for routing and confirmation.

Self-hosting does not remove Apple's iOS background restrictions.
For prompt iOS delivery, ntfy documents `upstream-base-url: https://ntfy.sh` on your own server.
The server's public `base-url` must match the phone's configured server address.
The relay receives poll requests with message IDs; the phone fetches the actual contents from your server.
Your ntfy server must be reachable by the phone for that fetch, including on mobile data.
This does not require exposing the companion's port or adding action callbacks.
Without the upstream relay, iOS alerts can take hours. Relay, push service, phone, and network failures can still delay alerts.
Read ntfy's [iOS relay guidance](https://docs.ntfy.sh/config/#ios-instant-notifications) and [known iOS issues](https://docs.ntfy.sh/known-issues/#ios-app-not-receiving-notifications-anymore).
Test on your actual phone with its normal lock, Focus, and battery settings before relying on alerts.

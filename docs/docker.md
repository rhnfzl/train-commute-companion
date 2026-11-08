# Run with Docker

The Docker app follows your usual NS train journey and sends alerts to a private Telegram chat. Alerts that matter ring; quiet ones arrive without sound (Telegram has no time-sensitive or critical tier). Buttons on the alerts and a few commands let you pick a train, say you are leaving, or skip a day.

Data: NS.

For multiple travellers, use the [separate household companions](household.md) recipe. Each companion has its own bot, location endpoint and stored data.

## What you need

- An NS API key. You bring your own; none is bundled.
- A Telegram bot token. Create a bot with BotFather and copy the token.

## First run

Run every command below from the `examples/` directory of this checkout, where the compose file builds the image from the checkout above it. Your `.env` and `config/` stay in `examples/` and are never committed.

1. Create `examples/.env` holding `NS_API_KEY` and `TELEGRAM_BOT_TOKEN`. Leave `TELEGRAM_CHAT_ID` out for now. Then copy the example profile:

   ```
   cd examples
   mkdir config
   cp profiles.yaml config/
   ```

2. Start it with `docker compose up -d --build`, then send the bot a message from a **private chat**. A group is refused at startup: every member could act and share location.
3. The log (`docker compose logs tcc`) shows a line with the chat id. Put it in `.env` as `TELEGRAM_CHAT_ID` and apply it with `docker compose up -d --force-recreate tcc` (a plain `restart` keeps the old environment).
4. In `config/profiles.yaml`, replace `AAA` with your own station codes and check the file:

   ```
   docker compose run --rm tcc tcc check-profile /config/profiles.yaml
   ```

5. Send `/start` to the bot and confirm both test alerts (one loud, one silent). Setup is complete only then, and `/health` stays 503 until you do.

## Commands and buttons

- `/status`: the journeys being followed now
- `/choose [profile] [outbound|return] HH:MM [train number] [YYYY-MM-DD]`: lock that train
- `/leaving [profile] [outbound|return]`: I'm leaving now
- `/skip [profile] [outbound|return|day] [YYYY-MM-DD]`: not travelling, alerts for it stop
- `/test`: send the loud and the silent test alert (and test each webhook)
- `/help`: this list

The same actions are buttons under the alerts and the `/status` reply.

## Settings

Everything comes from the environment (`.env`):

| Variable | Meaning |
| --- | --- |
| `NS_API_KEY` | your NS key |
| `TELEGRAM_BOT_TOKEN` | the bot token |
| `TELEGRAM_CHAT_ID` | the one chat allowed to talk to the bot; must be a private chat, a group id is refused |
| `OWNTRACKS_SECRET` | optional, 16+ printable ASCII characters without spaces; turns on the OwnTracks endpoint |
| `TCC_HEALTH_TOKEN` | optional, 16+ printable ASCII characters without spaces; lets a caller with `Authorization: Bearer <token>` read the full `/health` report |
| `TCC_LOOKAHEAD_AT` | when the planned-works look-ahead runs, `HH:MM`, default `06:00` |
| `TCC_WEBHOOK_URLS` | optional; webhook URLs separated by spaces or commas, for example `http://localhost:8644/webhooks/commute` |
| `TCC_WEBHOOK_SECRET` | required with `TCC_WEBHOOK_URLS`, 16+ characters; signs every webhook post |
| `TCC_WEBHOOK_TOKEN` | optional bearer token sent with every webhook post (for OpenClaw), 16+ printable ASCII characters without spaces |

## Health

`GET /health` on port 8080 answers 200 when all is well and 503 otherwise. It shows whether the runtime is alive and started, whether the state file writes, whether NS rejected the key, whether a profile was refused, how many alerts are undelivered, whether delivery was confirmed, and each journey's freshness. Every caller gets that status code and just `{"ok": true}` or `{"ok": false}`. The full report needs the header `Authorization: Bearer <TCC_HEALTH_TOKEN>` (set `TCC_HEALTH_TOKEN` first). The built-in Docker health check reads only the status code and needs no token.

A machine that is off cannot warn about itself, so point an external uptime check at `/health`. Keep that on your home network and never forward port 8080 as a whole. For OwnTracks, expose only `/owntracks` (see its page).

## Today web status

Open `http://127.0.0.1:8080/today` to read current journeys, NS data freshness and service warnings. Unselected replacement plans remain proposals. Followed recovery shows explicit user-confirmed progress. Refresh the page manually for a new snapshot.

Set `TCC_HEALTH_TOKEN` in `.env` to enable the page, then apply it with `docker compose up -d --force-recreate tcc`. Without the token, `/today` returns 404.

The browser asks for HTTP Basic credentials. Use any user name and your `TCC_HEALTH_TOKEN` as the password. Clients can instead send `Authorization: Bearer <TCC_HEALTH_TOKEN>`.

The page has no journey actions or JavaScript. It does not put tokens in links, page content or application storage. Never put the token in a URL. Avoid saving the password on a shared device.

Use TLS for remote browser access. The token grants administrator access to every profile in this companion. It does not provide separate private accounts. The page sends no-store headers; do not configure a proxy to cache it.

For separate household companions, use each owner's local port from the [household guide](household.md). Docker host administrators can access both companions. A shared Home Assistant instance also remains a shared trust boundary.

### Save one journey to a calendar

Today shows **Download this journey to calendar** for each usable selected journey with complete planned times. Click that link to download one `.ics` file, then open it in your calendar app.

The file contains one tentative saved plan. It does not create a recurring commute or export retained alternatives. The planned departure and arrival remain planned times, even when NS reports different forecasts.

The description identifies NS and the last source check. It states that the file is not live and does not prove attendance, boarding or actual arrival. A fixed link opens the NS planner for a new check.

Cancelled, skipped, uncertain or incomplete journeys have no download link. A recovery selection also disables original-train exports, including after recovery ends. The authenticated download checks the current journey again and returns 404 if it no longer qualifies.

Downloads use the same token and browser login as Today. URLs contain an opaque journey identifier and never contain credentials. The companion does not contact a calendar account or publish the file. This is a manual snapshot, not calendar synchronization.

The file uses UTC event dates and RFC 5545 text escaping and line folding. See the [iCalendar specification](https://www.rfc-editor.org/rfc/rfc5545.txt).

### Share a saved route preview

Choose **Download a route preview to share** beside a usable journey on Today. Open or forward the downloaded HTML file.

The preview includes only that journey's station codes, train numbers, planned dates and times, and export time. It excludes profile names, identifiers, phone locations, credentials, alerts and other journeys. It contains no scripts, remote images or live requests.

Anyone with the downloaded file can read its travel route and dates without a login. Share it deliberately. The preview is a saved timetable, not live status or proof of travel. The private download URL still requires your Today login. The companion does not publish an anonymous URL.

## Webhooks

With `TCC_WEBHOOK_URLS` set, every alert is also posted as signed JSON to each URL; [Webhooks](webhooks.md) has the payload, the headers and how to verify them. The payload carries profile names, stations and times, so prefer a receiver on the same machine, and use https for one anywhere else. `/test` also sends a signed loud and a signed silent test to each webhook and replies with one line per webhook saying whether it accepted both; only your tap on the phone test confirms delivery. A webhook that is down never holds back a phone alert and never turns `/health` to 503: the full report's `webhooks_failing` is `true` while some webhook URL is resting after a failed post and `null` without webhooks. Like every other detail it shows only in the full report, with `TCC_HEALTH_TOKEN`.

## Good to know

- `/data` must be one volume: atomic writes and the `.started` marker live there. Run one container per state file.
- A rejected NS key is retried with backoff (2, 5, 15, then 30 minutes). Fix the key in `.env` and apply it with `docker compose up -d --force-recreate tcc`.
- An alert can repeat after a crash between sending and saving (delivery is at least once).
- `docker stop` is safe; the app exits cleanly.

## MCP

A local MCP client such as Claude Code or Claude Desktop can read what `/status` shows through three read-only tools. There is no network port for it; see [MCP](mcp.md).

## Location

Location is optional and off by default.

- [Telegram live location](location-telegram.md): stations only.
- [OwnTracks](location-owntracks.md): also knows when you left home or reached work.

## Holiday pauses and crowd forecasts

Add [holiday pause ranges](holiday-pauses.md) to the shared profile file to suspend new journeys and resume automatically. Telegram status and alternatives include [NS crowd forecasts](crowd-forecasts.md) when available.

## Email copies

The Docker app can send native email copies alongside Telegram through certificate-verified SMTP SSL or STARTTLS. Configure `SMTP_HOST`, `SMTP_SECURITY`, `SMTP_FROM`, and `SMTP_TO`; add `SMTP_PORT`, `SMTP_USERNAME`, and `SMTP_PASSWORD` when required. [Email copies](email.md) explains setup, privacy, retries, and the separate email test command.

Individual notification categories can use email only. Telegram remains required for setup and phone delivery confirmation.

### Follow a recovery journey

Open `/status` and choose **Review recovery starting place** for an available NS replacement plan.
Select **I am at** only for your exact physical stop. The message shows its name, coordinates, and physical ID.
This choice refreshes the route. It does not start following it.

Read the refreshed offer before choosing **Follow this recovery**. Following accepts the displayed planned-only times and unverified accessibility and assistance.
Phone delivery confirmation through `/test` remains required.

Use **I boarded** and **I reached** buttons to report each vehicle and walking step.
Only these confirmations change progress. Time, Telegram location, and forecast arrivals do not confirm boarding or arrival.
The status shows the last confirmed place. Manual confirmations remain available when recovery data cannot refresh.
Review the NS planner when the route changes or data becomes stale. Use **Stop recovery** to end this recovery selection.

Old buttons refuse changes. Repeated delivery of the same Telegram callback does not advance progress twice.
Recovery selection and confirmations survive restart after a successful state save.
The Today page and MCP tools show recovery progress without action buttons.

## Weekly works summary

An optional [weekly planned-works digest](weekly-works-digest.md) checks the next seven calendar days for each profile.
Configure its day and time in the portable profile YAML. The summary uses the existing planned-works category and channel settings.
It labels missing evidence as unknown and expires before an obsolete summary can replay the next day.

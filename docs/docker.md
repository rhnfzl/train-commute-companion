# Run with Docker

The Docker app follows your usual NS train journey and sends phone alerts through ntfy or Telegram.
ntfy needs no bot setup. Telegram adds buttons and commands to pick a train, say you are leaving, or skip a day.
Loud and quiet delivery requests depend on your phone settings. Neither transport provides guaranteed critical alerts.

Data: NS.

[Commute weather](weather.md) uses keyless station forecasts. Set the optional `KNMI_API_KEY` in `.env` for official warnings.

For multiple travellers, use the [separate household companions](household.md) recipe.
Each companion needs its own private phone destination, location endpoint, and stored data.

## What you need

- Docker with Compose. The image runs on amd64 and arm64, so a Raspberry Pi with a 64-bit OS works too.
- Your own NS API key. None is bundled. [Get an NS API key](ns-api-key.md) shows how.
- Your own [ntfy topic and access token](ntfy.md), or a Telegram bot token from BotFather.

## Install and set up in Telegram

The installer asks for each traveller's own NS key and bot token. It starts private Docker projects with the Compose file and image from the same release. Docker Buildx resolves the published image digest so both services use the same image.

Set `version` to the number of a [release](https://github.com/rhnfzl/train-commute-companion/releases) that includes the installer. Use the release number without the `v`.

~~~sh
version=X.Y.Z
installer=$(mktemp)
curl -fsSL -o "$installer" "https://raw.githubusercontent.com/rhnfzl/train-commute-companion/v$version/scripts/install.sh" && bash "$installer" "$version"
~~~

By default, the installer creates `train-commute-companion` in the current directory. Pass an empty destination folder as the second argument to choose another location.

1. Run the log command printed for your traveller.
2. Open the private claim link or scan the terminal QR. Press **Start** in Telegram.
3. Send `/setup`. Choose your stations, commute days, and departure times. **Make this mine** previews a weekday sample for your stations.
4. Review the next commute's trains, platforms and changes. Adjust check-in lead times and delay thresholds if needed. Tap **Save commute**.
5. Confirm one loud and one silent test alert with **I got both**. Send `/test` to request another pair.

The wizard creates `/data/profiles.yaml` inside the Docker data volume when that file is absent. It previews train-only trips within an hour of each requested departure. The profile starts on the date shown. If NS cannot supply both trips, change the times or try again.

Use manual setup for a night shift or an existing profiles file. Keep the generated `.env` files and logs private.
Send `/cancel_setup` to close the wizard and discard the draft.

## First run

For phone alerts without Telegram or Home Assistant, follow [ntfy phone push](ntfy.md) after downloading the files below.
With ntfy configured, leave `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` blank. It needs no Telegram setup link.
The steps below describe Telegram setup.

You need no clone and no build. Download the compose file, settings template and example profile:

```
mkdir tcc && cd tcc
mkdir config
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/rhnfzl/train-commute-companion/main/examples/docker-compose.yml
curl -fsSL -o .env.example https://raw.githubusercontent.com/rhnfzl/train-commute-companion/main/examples/.env.example
curl -fsSL -o config/profiles.yaml https://raw.githubusercontent.com/rhnfzl/train-commute-companion/main/examples/profiles.yaml
cp .env.example .env
chmod 600 .env
```

Run every command below from this folder. Keep `.env` and `config/` private.

1. Edit `.env` with your `NS_API_KEY` and `TELEGRAM_BOT_TOKEN`. Use the primary key of the NS-App product. Leave `TELEGRAM_CHAT_ID` blank.
2. Start it with `docker compose up -d`, then read `docker compose logs tcc`.
3. Open the private Telegram setup link, or scan its terminal QR, and press **Start** in Telegram. Use your own private chat.
4. In `config/profiles.yaml`, replace `AAA` and the example stations with your own and check the file:

   ```
   docker compose run --rm tcc tcc check-profile /config/profiles.yaml
   ```

5. Confirm both test alerts (one loud, one silent) with **I got both**. Send `/test` if you need another pair. `/health` stays 503 until confirmation.

The setup link claims this bot once and saves your private chat in `/data/telegram.json` before routing notifications.
Restarts reuse that binding without an environment edit or container recreation. Groups, channels, wrong codes and reused links cannot replace it.
The link expires after 15 minutes and becomes invalid on restart. Restart the unclaimed container to print a new link.
QR generation runs locally and never sends the link to a QR service. Anyone with the unexpired link can claim the bot first.
Keep setup logs private and use a separate bot for each traveller. Telegram setup retries after connection failures independently of the train watchdog.
An unreadable saved Telegram state disables automatic claiming and leaves the original file untouched. Restore a valid backup or configure a manual chat.

For manual setup, set a known positive private `TELEGRAM_CHAT_ID` in `.env`, then run `docker compose up -d --force-recreate tcc`.
You can find the ID by sending this bot a private message and reading the ignored-chat line in its log.
Send `/start` from that chat and confirm both test alerts. Group and channel IDs are refused.
An explicit chat overrides the saved claim while configured. Removing it restores the claimed chat. Delivery confirmation belongs to one bot and chat.
Changing destinations requires confirmation there. Confirmation from another chat never transfers.

Telegram's [private start links](https://core.telegram.org/bots/features#deep-linking) deliver the setup code with `/start`.

On first startup, the log also prints a generated `TCC_HEALTH_TOKEN` for full health details and the Today page.
Store it in a password manager. The app saves it privately as `/data/health-token` and reuses it without printing it on restart.
Treat these setup logs as secrets. Docker host administrators and anyone who can read the logs can use this token.
If the saved file is invalid, readable by other users, or a symlink, startup refuses. Storage failures also stop startup.
Check the volume and owner-only file permissions. An explicit `TCC_HEALTH_TOKEN` in `.env` overrides the saved token.

## Update or pin a version

The compose file uses the `latest` image. Update with `docker compose pull` and then `docker compose up -d`.

For a stable setup, pin a version instead. In `docker-compose.yml`, replace `latest` in the `image:` line with a release number without the `v`, such as `X.Y.Z`. Change it when you choose to update.

## Build from source

To build the image yourself, clone the repository and run every command from the checkout's `examples/` directory:

```
git clone https://github.com/rhnfzl/train-commute-companion.git
cd train-commute-companion
cd examples
mkdir config
cp profiles.yaml config/
cp .env.example .env
chmod 600 .env
```

Git ignores `.env` and `config/` there. Build the image under the name the compose file uses:

```
docker build -t ghcr.io/rhnfzl/train-commute-companion:latest ..
```

Then follow the first run from step 1. After each update of the checkout, build again and run `docker compose up -d`. Do not run `docker compose pull`: it replaces your build with the ready-made image.

## Commands and buttons

- `/status`: the journeys being followed now
- `/plan`: start guided travel setup
- `/plans`: view, edit or cancel saved travel plans
- `/agents`: review or change which connected agents can manage plans
- `/preferences`: view confirmed travel preferences
- `/remember`: set a travel preference for a route
- `/forgetpref NAME`: remove a confirmed preference by name
- `/choose [profile] [outbound|return] HH:MM [train number] [YYYY-MM-DD]`: lock that train
- `/leaving [profile] [outbound|return]`: I'm leaving now
- `/skip [profile] [outbound|return|day] [YYYY-MM-DD]`: not travelling, alerts for it stop
- `/stations`: the boarding stations the companion remembers
- `/forget STATION`: stop remembering that boarding station
- `/test`: send the loud and the silent test alert (and test each webhook)
- `/help`: this list

The same actions are buttons under the alerts and the `/status` reply. A question about [another boarding station](other-boarding-station.md) shows only its own answer buttons.

Every tap gets a reply that names the button and says what it did, for example "I got both: Delivery confirmed. Setup is complete." When the tap takes effect, that button goes away and the others under the message stay, so "Not travelling" still works after "I'm leaving". A one-time choice, such as "I got both", an arrival confirmation or the answer to a question, takes all of its buttons with it. A refused tap keeps them, so you can try again.

A recovery step removes both old progress and stop controls for that step. The reply carries the current controls. If the reply cannot be delivered, the original buttons stay.

## Plan a trip in Telegram

Send `/plan`, then choose **New plan**. Enter the stations and the first travel date. For a weekly plan, choose the weekdays and the interval in weeks. For a monthly plan, choose a weekday of the month, such as the first Monday. Set a last date, choose **After N trips** (1 to 366), or choose **Ongoing**. Skipped dates do not count toward the trip limit. A return belongs to its outbound commute date, even when it leaves after midnight.

Choose an exact outbound train, a departure window, an arrival deadline or **When I leave**. A deadline by itself searches the three hours leading up to it. Use a departure window to search earlier. You can add a same-day or next-day return. Use **I'm leaving** to start a **When I leave** journey.

The companion shows any overlap with another plan before saving. Choose how to resolve the overlap, then check the summary and tap **Save plan**. A temporary replacement affects the dates it runs. Your usual commute resumes on other dates.

The saved message gives you an **Undo** button. Use `/plans` later to inspect the dates, edit the plan or cancel it. You can do all of this with language mode off.

### Remember travel preferences

Use `/remember Utrecht office | RTD | UT | 09:00 | leave` to set an arrival deadline and pick the return when you leave. Enter the name, origin and destination, then the arrival time and return preference. Use `-` to leave one timing field unset. Confirm the change in Telegram.

Use **Use a saved preference** in `/plan` to start with a saved route and timing options. Use `/preferences` to see what is saved or `/forgetpref Utrecht office` to review and remove it. If the companion notices repeated choices, it can ask whether to remember them. Only a confirmed preference becomes a default.

## Optional plain language

Set `TCC_TRAVEL_LANGUAGE=agent` to send trip requests to a compatible agent endpoint. Set it to `model` to use a chat-completions-compatible model endpoint. The default is `off`.

Set `TCC_TRAVEL_LANGUAGE_URL` to the full endpoint URL. Use HTTPS for a hosted endpoint. To use bearer authentication, set `TCC_TRAVEL_LANGUAGE_TOKEN` in your private `.env`. In model mode, also set `TCC_TRAVEL_LANGUAGE_MODEL` to the model name your provider expects.

Apply the settings with `docker compose up -d --force-recreate tcc`. In your private Telegram chat, close any guided setup and type a request. For example, “Utrecht every other Tuesday through March, arrive by 09:00 and pick my return when I leave.”

The endpoint receives your trip request and travel context, which can include saved plans and preferences. Choose an endpoint you trust with that data. The companion checks the result and asks about unresolved details or conflicts. Language plan changes default to **Ask**. Choose **Allow once**, **Always allow**, or **Deny** on the proposed change. You can change the **Telegram language** policy in `/agents`. Preference changes still require review.

In agent mode, the companion sends a JSON POST with `version: 1`, `text`, `context` and `change_schema`. Configure your agent's endpoint to return one JSON object. Return `{"change": {...}}` for a proposed plan or `{"question": "..."}` to ask for a detail. In model mode, the standard `choices[0].message.content` field must contain that JSON object. Configure the Telegram language endpoint separately from the [MCP connection](mcp.md).

## Settings

Everything comes from the environment (`.env`):

| Variable | Meaning |
| --- | --- |
| `NS_API_KEY` | required, the primary key of the NS-App product; see [NS API key](ns-api-key.md) |
| `TELEGRAM_BOT_TOKEN` | the bot token; optional when ntfy is configured |
| `TELEGRAM_CHAT_ID` | optional manual override; blank uses the saved private claim or prints a one-time setup link and QR |
| `OWNTRACKS_SECRET` | optional, 16+ printable ASCII characters without spaces; turns on the OwnTracks endpoint |
| `TCC_HEALTH_TOKEN` | optional override, 16+ printable ASCII characters without spaces; otherwise generated and saved in `/data/health-token`; grants full `/health` and Today access |
| `TCC_LOOKAHEAD_AT` | when the planned-works look-ahead runs, `HH:MM`, default `06:00` |
| `TCC_NTFY_TOPIC` | optional random private topic, 32-64 letters, digits, `_` or `-`; requires your access token |
| `TCC_NTFY_TOKEN` | your ntfy user access token; required with the topic |
| `TCC_NTFY_SERVER` | optional HTTPS server root, default `https://ntfy.sh`; no path, credentials, query or fragment |
| `TCC_NTFY_ENABLED` | `true` by default when configured; `false` disables ntfy while retaining valid settings; another phone transport is required |
| `TCC_PROFILES` | profile file path inside the container, default `/config/profiles.yaml` |
| `TCC_STATE_DIR` | writable persistent state directory, default `/data`; also holds the generated health token |
| `TCC_WEBHOOK_URLS` | optional; webhook URLs separated by spaces or commas, for example `http://localhost:8644/webhooks/commute` |
| `TCC_WEBHOOK_SECRET` | required with `TCC_WEBHOOK_URLS`, 16+ characters; signs every webhook post |
| `TCC_WEBHOOK_TOKEN` | optional bearer token sent with every webhook post (for OpenClaw), 16+ printable ASCII characters without spaces |
| `TCC_MCP_AGENTS` | optional JSON object of agent names and distinct random tokens, 32 to 128 characters each |
| `TCC_MCP_REMOTE_PORT` | optional port for remote MCP, unset by default |
| `TCC_MCP_REMOTE_BIND` | remote MCP bind address, defaults to `127.0.0.1` |
| `TCC_MCP_REMOTE_ALLOWED_HOSTS` | comma-separated host addresses accepted by remote MCP, including any non-default port |
| `TCC_TRAVEL_LANGUAGE` | `off` by default, or `agent` or `model` |
| `TCC_TRAVEL_LANGUAGE_URL` | full agent or chat-completions endpoint URL |
| `TCC_TRAVEL_LANGUAGE_TOKEN` | optional bearer token for that endpoint |
| `TCC_TRAVEL_LANGUAGE_MODEL` | model name, required in model mode |

## Health

`GET /health` on port 8080 answers 200 when all is well and 503 otherwise. Every anonymous caller receives only `{"ok": true}` or `{"ok": false}`.
Use `Authorization: Bearer <token>` with the generated token or your explicit `TCC_HEALTH_TOKEN` for the full report.
It includes runtime, storage, NS authentication, profile, delivery and journey freshness details.
The built-in Docker health check reads only the status code and needs no token.

A machine that is off cannot warn about itself, so point an external uptime check at `/health`. Keep that on your home network and never forward port 8080 as a whole. For OwnTracks, expose only `/owntracks` (see its page).

## Today web status

Open `http://127.0.0.1:8080/today` to read current journeys, NS data freshness and service warnings. Unselected replacement plans remain proposals. Followed recovery shows progress you explicitly confirmed.

Today refreshes once a minute while open. It reads the companion's current snapshot and makes no extra NS requests. Select **Refresh status** to update now or **Pause auto-refresh** to stop automatic updates.

In a browser with home screen support, sign in to Today and choose **Add to Home Screen** or **Install app**. The shortcut opens the same private page. Your browser may ask you to sign in again. The shortcut needs a connection to your companion. [Web App Manifest specification](https://www.w3.org/TR/appmanifest/)

`alerts.live_today` defaults to `true`. If any profile on the page sets it to `false`, Today offers manual refresh and omits the home screen manifest link for the whole page.

The generated health token enables the page automatically. You can override it with `TCC_HEALTH_TOKEN` in `.env`.
Apply an override with `docker compose up -d --force-recreate tcc`.

The browser asks for HTTP Basic credentials. Use any user name and the generated or explicit health token as the password.
Clients can instead send `Authorization: Bearer <token>`.

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

## Pinned Telegram status

After you confirm the loud and quiet tests, the companion adds one status pin in the bound private Telegram chat. It silently sends and pins the first message, then edits it when journey details change. The pin includes train numbers, times, platforms, delays and service warnings. Send `/status` for the full report.

```yaml
alerts:
  telegram_status_pin: false
```

`alerts.telegram_status_pin` defaults to `true`. Set it to `false` to omit a profile from the pin. When every profile is off or removed, the companion clears its own status text and unpins it, without creating a new one. If Telegram refuses the edit, the companion still tries to unpin its message. Other pins stay in place. [Telegram Bot API](https://core.telegram.org/bots/api#pinchatmessage)

To remove all profiles from a running app, replace the file contents with `profiles: []`. After reload, the next status update clears the pin. Invalid profile edits keep the previous configuration active.

If you change the bot or chat, confirm delivery again for the new destination. Remove an old pin from a previous destination in Telegram if you no longer want it there.

## Webhooks

With `TCC_WEBHOOK_URLS` set, every alert is also posted as signed JSON to each URL; [Webhooks](webhooks.md) has the payload, the headers and how to verify them. The payload carries profile names, stations and times, so prefer a receiver on the same machine, and use https for one anywhere else. `/test` also sends a signed loud and a signed silent test to each webhook and replies with one line per webhook saying whether it accepted both; only your tap on the phone test confirms delivery. A webhook that is down never holds back a phone alert and never turns `/health` to 503: the full report's `webhooks_failing` is `true` while some webhook URL is resting after a failed post and `null` without webhooks. Like every other detail it shows only in the full report, with `TCC_HEALTH_TOKEN`.

## Good to know

- `/data` must be one volume: atomic writes and the `.started` marker live there. Run one container per state file.
- Startup checks the NS key through the existing station-list request. Rejections appear in the log even before Telegram setup.
- A rejected NS key is retried with backoff (2, 5, 15, then 30 minutes). Station-list retries also yield one poll interval to live requests. Use the primary key of the NS-App product ([key guide](ns-api-key.md)). Fix `.env` and apply it with `docker compose up -d --force-recreate tcc`.
- An alert can repeat after a crash between sending and saving (delivery is at least once).
- `docker stop` is safe; the app exits cleanly.

## MCP

A local MCP client can read what `/status` shows through three read-only tools. You can connect an agent to manage travel plans and preferences, or enable an authenticated connection from another machine on your encrypted private network. See [MCP setup](mcp.md).

## Location

Location is optional and off by default.

- [Telegram live location](location-telegram.md): stations only.
- [OwnTracks](location-owntracks.md): also knows when you left home or reached work.

## Holiday pauses and crowd forecasts

Add [holiday pause ranges](holiday-pauses.md) to the shared profile file to suspend new journeys and resume automatically. Telegram status and alternatives include [NS crowd forecasts](crowd-forecasts.md) when available.

## Email copies

The Docker app can send native email copies alongside ntfy or Telegram through certificate-verified SMTP SSL or STARTTLS. Configure `SMTP_HOST`, `SMTP_SECURITY`, `SMTP_FROM`, and `SMTP_TO`; add `SMTP_PORT`, `SMTP_USERNAME`, and `SMTP_PASSWORD` when required. [Email copies](email.md) explains setup, privacy, retries, and the separate email test command.

Individual notification categories can use email only. Confirm phone delivery through ntfy's local test or Telegram's `/test`.

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

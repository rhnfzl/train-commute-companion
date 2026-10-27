# Run with Docker

The Docker app follows your usual NS train journey and sends alerts to a private Telegram chat. Alerts that matter ring; quiet ones arrive without sound (Telegram has no time-sensitive or critical tier). Buttons on the alerts and a few commands let you pick a train, say you are leaving, or skip a day.

Data: NS.

## What you need

- An NS API key. You bring your own; none is bundled.
- A Telegram bot token. Create a bot with BotFather and copy the token.

## First run

1. Copy `examples/docker-compose.yml` next to a new `.env` file holding `NS_API_KEY` and `TELEGRAM_BOT_TOKEN`. Leave `TELEGRAM_CHAT_ID` out for now.
2. Start it with `docker compose up -d --build`, then send the bot a message from a **private chat**. Never use a group: every member could act and share location.
3. The log shows a line with the chat id. Put it in `.env` as `TELEGRAM_CHAT_ID` and restart.
4. Create a `config/` directory and copy `examples/profiles.yaml` into it. Replace `AAA` with your own station codes and check the file:

   ```
   docker compose run --rm tcc tcc check-profile /config/profiles.yaml
   ```

5. Send `/start` to the bot and confirm both test alerts (one loud, one silent). Setup is complete only then, and `/health` stays 503 until you do.

## Commands and buttons

- `/status`: the journeys being followed now
- `/choose [profile] [outbound|return] HH:MM [train number] [YYYY-MM-DD]`: lock that train
- `/leaving [profile] [outbound|return]`: I'm leaving now
- `/skip [profile] [outbound|return|day] [YYYY-MM-DD]`: not travelling, alerts for it stop
- `/test`: send the loud and the silent test alert
- `/help`: this list

The same actions are buttons under the alerts and the `/status` reply.

## Settings

Everything comes from the environment (`.env`):

| Variable | Meaning |
| --- | --- |
| `NS_API_KEY` | your NS key |
| `TELEGRAM_BOT_TOKEN` | the bot token |
| `TELEGRAM_CHAT_ID` | the one chat allowed to talk to the bot |
| `OWNTRACKS_SECRET` | optional; turns on the OwnTracks endpoint |
| `TCC_LOOKAHEAD_AT` | when the planned-works look-ahead runs, `HH:MM`, default `06:00` |

## Health

`GET /health` on port 8080 answers 200 when all is well and 503 otherwise. It shows whether the runtime is alive and started, whether the state file writes, whether NS rejected the key, whether a profile was refused, how many alerts are undelivered, whether delivery was confirmed, and each journey's freshness.

A machine that is off cannot warn about itself, so point an external uptime check at `/health`. Keep that on your home network: `/health` lists your journeys, so never forward port 8080 as a whole. For OwnTracks, expose only `/owntracks` (see its page).

## Good to know

- `/data` must be one volume: atomic writes and the `.started` marker live there. Run one container per state file.
- A rejected NS key is retried with backoff (2, 5, 15, then 30 minutes). Fix the key and restart.
- An alert can repeat after a crash between sending and saving (delivery is at least once).
- `docker stop` is safe; the app exits cleanly.

## Location

Location is optional and off by default.

- [Telegram live location](location-telegram.md): stations only.
- [OwnTracks](location-owntracks.md): also knows when you left home or reached work.

# Email copies

The Docker app can email commute alerts alongside Telegram. Email copies help you keep a searchable record of changes.

Telegram remains required for setup, interaction, and phone delivery confirmation. Individual [notification categories](notification-preferences.md) can use email only, after SMTP is configured. This does not remove the phone setup requirement.

## Configure SMTP

Set these variables in `examples/.env`, then recreate the container:

| Variable | Meaning |
| --- | --- |
| `SMTP_HOST` | SMTP hostname, without a URL scheme |
| `SMTP_SECURITY` | Required: `ssl` or `starttls` |
| `SMTP_PORT` | Optional: defaults to 465 for SSL, 587 for STARTTLS |
| `SMTP_FROM` | One ASCII sender address, without a display name |
| `SMTP_TO` | One ASCII recipient address, without a display name |
| `SMTP_USERNAME` | Optional SMTP username; requires `SMTP_PASSWORD` |
| `SMTP_PASSWORD` | Optional SMTP password; requires `SMTP_USERNAME` |

The server must offer AUTH PLAIN when credentials are configured. Authentication occurs only after certificate-verified TLS. STARTTLS must be advertised and must succeed. Plaintext SMTP is refused. Partial configuration refuses startup and names the missing or invalid variable.

Keep credentials in your private environment file. Never commit that file. The app does not save SMTP credentials in its state file.

Run from `examples/`:

```sh
docker compose up -d --force-recreate tcc
docker compose run --rm tcc python -m tcc_app.email_settings --test
```

The test sends one loud and one silent email. Its output reports whether SMTP accepted each message. Check your inbox and spam folder. SMTP acceptance does not prove inbox arrival or phone sound. The labels in subjects do not change mail-app sound settings. Telegram `/test` still tests Telegram and configured webhooks separately.

## Delivery and privacy

Emails include the profile name and alert text. That text can identify stations, journey times, and travel patterns. Your SMTP provider and recipient mailbox receive this information. Use a mailbox you control.

A new email copy waits until its original recipient IDs are saved. If storage fails before that write, the copy stays held. This prevents a crash and recipient change from forwarding a fact already disclosed to another recipient.

Each destination has its own durable receipt. A failed email never repeats an accepted Telegram message or accepted webhook. Failed email copies retry after a 60-second rest. The shared auxiliary outbox keeps at most 200 copies and gives up two hours after the first attempt. Stale or superseded journey alerts can end earlier.

A crash between SMTP acceptance and saving its receipt can repeat an email. Retries keep the same Message-ID. Mail servers may still display duplicates.

The entire SMTP conversation has a 15-second deadline. Slow email delivery runs outside the phone delivery task. An email outage appears through the existing `webhooks_failing` field in the authenticated health report. It does not make the basic health status fail.

Each queued copy is bound to its original destination IDs. Changing SMTP settings does not forward existing copies to a new recipient. Only newly staged alerts use new destinations. Older copies without destination IDs are not forwarded to any auxiliary destination, because their original recipient scope is unknown.

This SMTP configuration belongs to the Docker app. Home Assistant retains its own notification services and configuration.

Disruption starts and clearances have independent receipts for each destination. Email can receive a clearance even when Telegram failed. A clearance goes only to destinations that accepted a start. Fresh, complete NS evidence and an active monitoring window are still required. A stopped or archived journey does not send later clearances.

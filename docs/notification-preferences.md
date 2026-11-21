# Notification preferences

A traveller can choose notification channels or disable alert categories for one profile. This reduces phone interruptions while keeping selected updates in email or webhooks.

## Delay thresholds

Choose when a train counts as delayed and how much its delay must change before another update:

```yaml
alerts:
  delay_min: 10
  delay_step_min: 5
```

Both settings default to 3 minutes. Each must be a positive whole number from 1 to 1440 minutes, matching the daily minute controls. Fractions, quoted numbers, booleans and null are rejected in YAML. The Home Assistant profile details form provides both controls.

`delay_min` applies to departure and arrival classification for that profile. At 10, a 9-minute delay sends no first delay alert. A 10-minute delay does. `delay_step_min` measures changes from the last stated delay in either direction. With a step of 5, a stated 10-minute delay updates at 15 minutes, but not at 14. Recovery to 1 minute or less still produces a recovery update.

Before the active point, a first delay alert requires at least `max(delay_min, 5)` minutes. Updates and recovery for a delay already stated retain their normal rules.

These settings do not suppress cancellations, cut-short journeys, broken or at-risk transfers, arrival-limit warnings, authentication failures or storage warnings. Other category and channel preferences still apply. Journey selection, catchability and transfer checks continue to use actual train times.

Delivery checks the current thresholds before retrying held delay alerts or auxiliary copies. Raising a threshold can drop a pending delay update. Accepted messages cannot be recalled. Existing stored profiles without either setting retain the three-minute defaults after restart.

Repeat updates below the first-alert threshold still use the repeat step. Held recovery updates also use the current step, except recovery to 1 minute or less.

Older held alerts do not record whether they protect an arrival deadline. When a deadline is configured, these alerts retain protection. This can also retain an older routine delay alert below the new threshold. Newly created alerts record the distinction.

Older pending recovery alerts also lack the previous delay. They remain eligible because the repeat step cannot be checked. Current alerts record that delay and use the current step.

## Disable categories

```yaml
alerts:
  disabled_categories: [crowding, routine]
```

The default is an empty list. All categories are enabled. Values must use the exact names below. Unknown categories and values other than lists are rejected.

The Home Assistant profile form provides the same category list. Choosing a category disables it for every destination for that profile.

| Category | Notifications |
| --- | --- |
| `journey` | Cancellation, cut-short journey, changed route, missed first train, no connection, restored train service, questions about another boarding station |
| `transfers` | Broken transfer, transfer at risk, missed transfer |
| `advice` | Leave now, withdrawn earlier-train advice, next connection |
| `delays` | Delay, nearly on time, back to normal |
| `platforms` | Platform change |
| `crowding` | Changed crowd forecast |
| `planned_works` | Upcoming works, adopted timetable changes, restored lookahead journeys |
| `disruptions` | Route disruption and its clearance |
| `routine` | All-good confirmation, at-station status, compensation reminders, daily commute brief |

A platform change is loud from 15 minutes before the expected departure and silent before that.

## Leave-by time

Check-in and pre-trip advice show **Leave by HH:MM** by default.
This is the earlier of planned and NS-expected departure, minus the direction's `buffer_min` travel time.
A delay never gives you a later leave-by time. Midnight and clock changes use elapsed minutes.

```yaml
alerts:
  leave_by: true
```

Set `leave_by: false` to hide this text and the Home Assistant status sensor's `leave_by` timestamp.
With fresh departure-station weather, `weather_allowance` suggests extra minutes in this text. It defaults to on and changes advice only.
Set `alerts.weather_allowance: false` to keep the usual leave-by time. See the [minute table and limits](weather.md#suggested-extra-time-before-departure).

Docker profiles also accept `alerts.live_today` and `alerts.telegram_status_pin`. Both default to `true`. These settings control [Today refresh and the home screen shortcut](docker.md#today-web-status) and [the Telegram status pin](docker.md#pinned-telegram-status).

The HA profile details form has the same **Show leave-by time** switch. It does not change notification timing.
Use per-direction `active_lead_min` for check-in timing. Its default is `buffer_min + 15` when omitted.

System health, missing train data, stopped monitoring, invalid configuration, rejected credentials and command feedback remain enabled. These messages explain when the companion cannot follow a journey reliably.

A train missing from NS data is reported after two polls in a row miss it while you follow the same train, so switching to another train never warns on its own. A stale or failing NS board is reported at once. "Visible again" follows only a "cannot see" message you actually received.

## Sent alert history

`alerts.alert_history` defaults to `false`. Set it to `true` to save recent accepted alerts locally. [View history and retention details](docker.md#recent-alerts)

## Quick trip checks

You can check the next trains between any two NS stations from the private Telegram chat or a Home Assistant action. Quick checks are enabled by default, including when you have no commute profile.

```yaml
alerts:
  quick_trip: false
```

Set `quick_trip: false` in any configured profile to disable checks for the whole running companion, including checks for other station pairs. The setting accepts YAML booleans `true` and `false`. The Home Assistant profile details form has the same **Allow quick trip checks** switch.

## Open the boarding station in Maps

Tap **Apple Maps** or **Google Maps** on a pre-trip phone alert or in Telegram `/status`.
With ntfy, the map links appear in the notification text.
The buttons open the NS station your current itinerary boards, including a different station you choose and supported train stations in a selected recovery.
Links contain only public coordinates from the NS station catalogue. The companion omits them when it cannot verify a current boarding station.
Set `alerts.navigate: false` to hide the buttons from new alerts and status messages.
An older message keeps the station shown when it was sent. Check the latest journey before using its map button.

Both `alerts.navigate` and `alerts.arrival_prompt` default to `true` and require Boolean values.
The [arrival question](compensation-reminders.md#confirm-a-suggested-arrival-time) also requires opt-in compensation reminders and primary delivery for the routine category.

## Checked alternatives

When an alert suggests a different train, the companion asks NS again before sending it.
The alert says how recently NS showed that connection running.
It gives the reported platform, or labels the scheduled platform when that is all NS provides.

```yaml
alerts:
  checked_alternatives: true
```

The setting defaults to on. The Home Assistant profile details form calls it **Check suggested trains**.
Set it to `false` to use the regular monitoring data without a dedicated check.

The check uses the shared NS request budget. Delivery channels share the result for up to two minutes while the journey evidence stays current.
If the check cannot confirm the train, alerts omit the suggestion.
When NS shows only cancellations among the train options checked for that part of the journey, the warning says so and refers you to the NS app.

## Repeated missed trains

When you stay at home (at work on the way back) past your train, the companion says "you missed" with the next train you can catch, and follows that one. Stay longer and each next train gets its own "you missed" message. By default every one of them makes a sound.

```yaml
alerts:
  quiet_repeat_misses: true
```

With `quiet_repeat_misses: true`, only the first of these messages that day makes a sound. Each later one from the same place still arrives, silently, also for a train you chose or one that replaced a cancelled train. A miss at the station stays loud. The default is `false`. The Home Assistant profile form has the same switch.

## Quiet early watch

The usual train is locked as soon as the window opens, with no extra NS requests. Until the active point, only unusual news is sent: a cancellation, a broken change, a train cut short, a changed route, a platform change, no connection found, a restored train, and a delay of at least `max(delay_min, 5)` minutes. A delay you already heard about keeps its usual updates, so a change or a recovery is still told, and it is not repeated at the active point. Smaller first delays, crowd forecasts, a transfer at risk, unclear NS data, a train missing from NS data, earlier-train advice and the "on time per NS" check-in wait for the active point. "Cannot see your train" can only start there too. A usual train that is not in NS data when the window opens is told at once, and the next good train is followed instead.

The active point is `active_lead_min` minutes before your first train, set per direction. The default is `buffer_min + 15`, when following used to start, so the check-in comes at the same time as before. It must be at least `buffer_min`. A value longer than the window's opening turns the quiet phase off. Messages from your location, such as a missed train at the station, are never held.

```yaml
outbound: {first_train: "07:25", buffer_min: 20, active_lead_min: 35}
```

The shared delivery check reads the current profile settings before every attempt. Disabling a category drops its pending primary notifications and auxiliary copies. This includes alerts held during quiet hours and pending warnings about an old route.

A message already accepted by a destination cannot be recalled. A request already in progress may finish after a settings change.

Enabling a category does not replay previously dropped messages. Existing alert history can suppress an unchanged fact until it changes or occurs again. Some active disruption notices and advice episodes can regenerate through their existing reconciliation rules. Treat enabling as a preference for future notifications, not a request for a current-state briefing. A recovery update can follow an initial alert that was suppressed.

The dashboard and current journey data remain available. Notification preferences do not change journey selection or monitoring.

## Choose channels

Use `alerts.channels` to choose channels for individual categories:

```yaml
alerts:
  disabled_categories: [routine]
  channels:
    journey: [primary, email]
    disruptions: [email, webhook]
    delays: [primary]
```

`primary` means the configured ntfy destination, Telegram chat, or Home Assistant phone.
In Docker, configured ntfy becomes primary; Telegram can still provide commands and answers.
`email` means native SMTP delivery in Docker. `webhook` means every configured webhook URL.
A category omitted from `channels` uses all configured channels. `disabled_categories` overrides channel choices.

Channel lists must contain at least one of `primary`, `email`, or `webhook`. Use `disabled_categories` to suppress a category entirely. Unknown categories and channels are rejected. Reliability and command feedback continue through all configured channels.

Every explicitly selected channel must be configured, including selections for disabled categories. Docker requires SMTP settings for `email` and webhook URLs for `webhook`. Home Assistant supports its phone and configured webhooks. It does not provide native SMTP delivery. The HA profile editor offers the channels configured for that integration.

Portable `tcc check-profile` checks syntax without host transport availability. The running host also checks availability. An invalid edit keeps the last valid profile. If a previously valid profile needs a transport removed from the host, affected deliveries remain held and configuration refusal is reported. Restoring the transport can resume still-relevant messages for their original recipients.

Private alerts wait until their initial recipient pin is saved. A full disk can therefore hold a new alert until storage recovers. The generic storage-failure warning can still reach the primary phone. An alert whose audience was already saved can continue delivery when a later receipt write fails.

Each alert pins its selected recipients before delivery. Later policy changes can remove recipients but cannot add recipients to that alert. New recipients receive future alerts. An unknown primary recipient keeps a pending phone slot while selected auxiliary recipients can receive their own copies. Setup must still bind and confirm the phone, including for email-only categories.

A failed primary delivery is independent of accepted auxiliary copies. An email-only category does not count as confirmed phone delivery. Disruption clearances go only to destinations that accepted a start, using independent receipt history. A later routing change cannot create clearance eligibility for a recipient that never saw the incident.

# Notification preferences

A traveller can choose notification channels or disable alert categories for one profile. This reduces phone interruptions while keeping selected updates in email or webhooks.

```yaml
alerts:
  disabled_categories: [crowding, routine]
```

The default is an empty list. All categories are enabled. Values must use the exact names below. Unknown categories and values other than lists are rejected.

The Home Assistant profile form provides the same category list. Choosing a category disables it for every destination for that profile.

| Category | Notifications |
| --- | --- |
| `journey` | Cancellation, cut-short journey, changed route, missed first train, no connection, restored train service |
| `transfers` | Broken transfer, transfer at risk, missed transfer |
| `advice` | Leave now, withdrawn earlier-train advice, next connection |
| `delays` | Delay, nearly on time, back to normal |
| `platforms` | Platform change |
| `crowding` | Changed crowd forecast |
| `planned_works` | Upcoming works, adopted timetable changes, restored lookahead journeys |
| `disruptions` | Route disruption and its clearance |
| `routine` | All-good confirmation |

System health, missing train data, stopped monitoring, invalid configuration, rejected credentials and command feedback remain enabled. These messages explain when the companion cannot follow a journey reliably.

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

`primary` means the configured Telegram chat or Home Assistant phone. `email` means native SMTP delivery in Docker. `webhook` means every configured webhook URL. A category omitted from `channels` uses all configured channels. `disabled_categories` overrides channel choices.

Channel lists must contain at least one of `primary`, `email`, or `webhook`. Use `disabled_categories` to suppress a category entirely. Unknown categories and channels are rejected. Reliability and command feedback continue through all configured channels.

Every explicitly selected channel must be configured, including selections for disabled categories. Docker requires SMTP settings for `email` and webhook URLs for `webhook`. Home Assistant supports its phone and configured webhooks. It does not provide native SMTP delivery. The HA profile editor offers the channels configured for that integration.

Portable `tcc check-profile` checks syntax without host transport availability. The running host also checks availability. An invalid edit keeps the last valid profile. If a previously valid profile needs a transport removed from the host, affected deliveries remain held and configuration refusal is reported. Restoring the transport can resume still-relevant messages for their original recipients.

Private alerts wait until their initial recipient pin is saved. A full disk can therefore hold a new alert until storage recovers. The generic storage-failure warning can still reach the primary phone. An alert whose audience was already saved can continue delivery when a later receipt write fails.

Each alert pins its selected recipients before delivery. Later policy changes can remove recipients but cannot add recipients to that alert. New recipients receive future alerts. An unknown primary recipient keeps a pending phone slot while selected auxiliary recipients can receive their own copies. Setup must still bind and confirm the phone, including for email-only categories.

A failed primary delivery is independent of accepted auxiliary copies. An email-only category does not count as confirmed phone delivery. Disruption clearances go only to destinations that accepted a start, using independent receipt history. A later routing change cannot create clearance eligibility for a recipient that never saw the incident.

# Weekly planned-works digest

The companion can send one weekly summary of planned works for each profile. Each summary shows checked routes and unknown coverage separately.
The digest is off by default. It uses the existing delivery channels and does not replace journey alerts or daily look-ahead.

Add this setting beside a profile's `outbound` and `return` settings:

```yaml
works_digest:
  day: fri
  time: "18:00"
```

Use `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, or `sun`. Quote the time as `HH:MM`.
Remove the setting, or set it to `null`, to turn the digest off.
Both Docker and Home Assistant read this portable YAML setting. The visual profile editor preserves it when editing other settings.

## Dates and evidence

The summary covers tomorrow through seven calendar days after the scheduled day. It lists both directions for every date.
Days without a scheduled commute say `not scheduled`. Extra dates override holiday skipping, as they do for normal journeys.
Pause ranges apply to actual service dates. An overnight return shows its separate service date.
Weekday departure overrides and dated learned timetables apply to the probes.

The digest requests a fresh planned-works list and dated route checks through the existing shared NS request budget.
It checks at most fourteen date and direction pairs. Each pair uses the existing bounded route probe queries.
One overall ninety-second deadline bounds the network work for each profile.
Live polling continues while the digest runs. Rate limits, timeouts, unavailable routes, mismatched source evidence, and unfinished checks remain unknown.

A named work appears only when its reported time interval and station identifier match a checked route.
Unresolved station identities prevent an all-clear result. Reported timetable effects do not establish their cause.
`No planned-works indication found in these checks` requires complete works coverage and clean route checks.
It is not a service guarantee. The message states when checks started; check NS again before travelling.

## Delivery and restart

The digest is silent and belongs to the `planned_works` notification category.
It uses every configured channel by default. Existing category controls can select primary phone delivery, email, or webhooks:

```yaml
alerts:
  channels:
    planned_works: [email]
```

That selection also applies to other planned-works notifications. Email must be configured on the host.
Docker still requires the initial Telegram phone delivery confirmation.
Disabling `planned_works` prevents digest requests and delivery.

The companion records the week and message together before sending. A failed state save prevents private digest delivery.
It can retry during the scheduled day after storage recovers. The same week does not repeat after a restart or backward clock change.
Each destination uses the existing durable receipt and recipient-pinning rules.
New recipients do not receive an already staged summary.

A digest runs after the chosen time on the chosen day. It does not catch up on a later day.
An undelivered summary expires at the next local midnight. Profile changes invalidate an older pending summary.
Unknown coverage is still a completed weekly summary. It does not trigger repeated weekly probes during the same week.
Spring clock gaps move a slot to the first valid local time. An autumn repeated hour uses its first occurrence.

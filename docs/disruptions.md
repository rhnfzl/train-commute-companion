# Active disruption context

The companion tells you when NS reports an incident involving stations on your followed route. It helps you check the wider situation without claiming your particular train is cancelled.

Active incident checks run after the journey requests, using the same NS request budget. They run only while an enabled, unpaused journey is within its monitoring window. Planned-works look-ahead remains separate.

## Notifications

A new relevant incident or a change to its public content produces a silent `disruption` alert. A successful, complete NS snapshot can produce one silent `disruption_cleared` alert when an incident disappears. That message says NS no longer lists the incident; it does not confirm your trains run.

Repeated snapshots and restarts do not repeat a delivered notice. Failed delivery can retry while its source state still holds. An incident that ends before its first warning reaches you does not produce an unexplained clear message.

Unknown or malformed feeds never establish a clear. Held notices are rechecked before delivery. If the source becomes unknown, outdated pending notices are dropped and can be queued again after a complete check.

## Status and automation

Telegram and local MCP status include active context. An unavailable check adds a warning that previous context may be stale. The same warning appears in every MCP tool that carries status warnings.

Each Home Assistant direction status sensor exposes `active_disruptions`, `disruptions_checked_at` and `disruptions_coverage`. The check timestamp records the most recent attempt, not a promise that it succeeded. Check coverage before using the context. Each context item contains the public NS incident ID, title, station identifiers and an optional NS link.

Alerts also use the existing HA event and signed webhook delivery. Their kinds are `disruption` and `disruption_cleared`. Use the profile and direction fields to route an alert.

## Limits

Station overlap means possible route impact. It is not evidence that a particular train fails. Your train-level cancellation, delay and transfer warnings remain independent.

A public NS banner without station information cannot be assigned reliably to your route. It is not inferred from its title. Check the NS app for such network-wide announcements. If a previously scoped incident loses its scope, its earlier context stays visible as unverified and no clear is inferred.

Normal polling and API backoff still apply. An offline host cannot check or deliver anything. An accepted phone push does not prove it was displayed.

Email, webhooks, and the primary phone track disruption acceptance separately. Only a destination that accepted a start can receive its clearance. A delayed start accepted after the feed cleared can create a targeted clearance while the source evidence remains fresh and complete. Monitoring still ends when the journey stops or its window closes.

Queued facts stay bound to their original recipients. A replacement phone or email recipient does not inherit old incident receipts. If a Home Assistant mobile notification service changes its underlying device, reload this integration to bind the new device. Older state files have no auxiliary receipt history; the app does not invent that history or transfer unknown primary receipts to a newly identified phone.

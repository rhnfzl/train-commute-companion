# Accessible journey preferences and boarding information

A traveller can request NS journeys that use the accessible-trip filter. The companion also shows reported assistance notes and the exit side.

Add this optional mapping to a profile:

```yaml
accessibility:
  require_accessible: true
```

The default is `false`. Only YAML booleans are accepted. Unknown fields are rejected. The Home Assistant profile form also exposes this preference.

Every journey search uses the preference, including transfer recovery, location searches, explicit choices, earlier-train advice and lookahead checks. The NS adapter sends `searchForAccessibleTrip=true` when enabled.

Changing the preference changes the route configuration. A journey already followed keeps its original preference until it ends. This preserves monitoring of trains already selected.

The filter asks NS for accessible journeys. It does not independently verify step-free access, a working lift, available assistance or wheelchair boarding.

NS reports assistance notes on journey endpoints. The adapter preserves the text of `ATTRIBUTE` notes with key `SS`. It does not interpret the text as an accessibility guarantee. Booking assistance remains a separate traveller action.

The adapter maps reported `LEFT` and `RIGHT` exit sides. Missing or unknown values stay unknown. Exit sides can change with train operations.

The public model exposes these values:

- `Stop.accessibility_notes`: reported assistance text, or an empty tuple.
- `Stop.exit_side`: `left`, `right`, or `None`.
- `LegView.boarding_notes`: assistance text at the boarding stop.
- `LegView.alighting_notes`: assistance text at the alighting stop.
- `LegView.exit_side`: the reported side at the alighting stop.

These fields use the newest provider answers. Older complete journey copies cannot retain stale boarding notes when a newer answer omits them. Conflicting exit sides from equally recent answers stay unknown.

The Home Assistant leg and next-connection attributes include these fields. Telegram status shows provider notes and exit sides. MCP alternatives preserve the structured fields. Old saved profiles and stops load with disabled preferences and unknown boarding data.

Public NS journey data does not establish lift status, platform access routes, assistance reservations or guaranteed boarding. These features require separate evidence or services.

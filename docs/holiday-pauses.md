# Pause a commute during a holiday

Set a date range to stop new journeys and advance warnings for that profile. Monitoring resumes automatically after the end date.

Add `pauses` alongside `days` in your profile:

```yaml
profiles:
  - id: "p1"
    name: "office_a"
    days: [tue, wed]
    pauses:
      - start: "2026-12-21"
        end: "2027-01-03"
    # Keep the profile's stations, journeys and other settings here.
```

Both dates are included. Ranges can overlap. Use `pauses: []` or remove the field when no ranges apply.

The dates refer to the day each journey starts. If a Tuesday return starts after midnight on Wednesday, a Wednesday pause blocks that return. A Tuesday pause does not block a return that starts on Wednesday.

A pause overrides `extra_dates`. Dutch public-holiday skipping and individual-day skip actions still work as before.

When a profile edit adds a pause, an unlocked journey is removed. A journey already locked to a train keeps its safety monitoring. Use the existing “not travelling” action to stop that journey too.

The next planned-works check skips paused journeys and selects later travel dates within its normal ten-day horizon. This does not extend that horizon.

The same profile file works in Home Assistant and Docker. Check edits with `tcc check-profile profiles.yaml`, then let the running companion reload the file.

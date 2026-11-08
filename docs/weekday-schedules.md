# Weekday departure times

One commute profile can use different usual departure times on different weekdays. This avoids separate profiles when the route stays the same.

```yaml
outbound:
  first_train: "08:12"
  first_train_by_weekday:
    tue: "09:12"
    thu: "07:42"
  transfers: [UT]
  buffer_min: 10
return:
  first_train: "17:52"
  first_train_by_weekday:
    tue: "18:40"
  buffer_min: 5
```

Unspecified weekdays use `first_train`. Keys must be `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, or `sun`. Times must be quoted `HH:MM` values. An empty mapping removes the overrides.

Transfers, transfer minimums, buffers, advice preferences, and return day offsets stay shared. Use separate profiles if these settings differ by weekday. The Home Assistant profile editor can set or clear weekday departures without changing other profile settings.

## Dates and monitoring windows

The override uses the original commute weekday. With a Tuesday commute and `return.day_offset: 1`, the Tuesday return override applies early Wednesday. It does not use the Wednesday override.

An extra travel date uses that date's weekday. Holiday pause ranges continue to apply to the actual service date, including an offset return.

The monitoring window moves with the departure. Its time before and after departure stays the same. A default window from `07:12` to `10:12` moves to `08:12` through `11:12` for a departure changed from `08:12` to `09:12`.

Windows can cross midnight. Clock-change nights keep the existing conservative policy: starts use the first occurrence of a repeated time, and ends use the last. Skipped times move to the first valid local time. The profile checker warns about configured weekday times and shifted window edges in the clock-change hour. Overlapping journeys are still rejected.

## Learned timetables

A direction with overrides learns a separate timetable for each commute weekday. A Tuesday observation cannot confirm or replace Wednesday's timetable. Two distinct service dates are still required to confirm a newly learned timetable.

A learned timetable contains every leg's actual planned departure, transfer pattern, and duration. The companion never shifts an old timetable to invent missing onward train times. A changed weekday starts with its configured first departure and no learned full timetable.

Look-ahead retains the existing budget: at most the next three eligible commute dates within ten days. Each weekday is judged against all requested probes for that weekday. Learning an infrequent weekday can therefore take longer.

When overrides are enabled, unchanged weekdays retain confirmed global timetable signatures, including existing adoptions. They receive fresh version IDs. Changed weekdays and unconfirmed legacy seeds start without a learned timetable. Editing one weekday preserves unaffected weekday histories and their confirmation evidence.

Removing all overrides from a direction starts a new uniform timetable history. It does not promote one weekday's learned timetable to every day. Changing stations, transfers, or return offsets also resets the affected direction's learned timetable.

Journeys already locked or retained keep their original route revision and full timetable. An edit does not move a train the traveller is already following. In-flight look-ahead results from an earlier configuration are rejected.

Older profile files and saved state remain readable. Without overrides, the existing uniform timetable behavior stays in place.

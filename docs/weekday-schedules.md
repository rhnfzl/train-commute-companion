# Schedules

Each direction runs in one of two modes. Fixed mode follows your usual train. It is the default, and existing profiles stay in fixed mode. Window mode follows any train between two times.

## Window mode

```yaml
outbound:
  mode: window
  window: ["07:00", "10:00"]
  buffer_min: 10
```

In window mode `window` holds the earliest and the latest departure. Both are included. The latest time is always required. `first_train` is optional.

TCC starts watching `active_lead_min` before the earliest departure (default `buffer_min` + 15). It follows the earliest good train in the window. If a train is missed, it follows the next one, like fixed mode does. If no train leaves in the window, it says so once.

There is no usual train on a window day. So look-ahead does not probe that day, no timetable is learned or adopted, and the earlier train advice is off. The weekly works digest still runs. Compensation reminders use the train TCC followed.

## Train choice

`pick` chooses how a window day picks its train:

- `earliest` (default): the earliest good train. A missed train makes a sound.
- `earliest_silent`: the earliest good train. A missed first train is silent. A missed change still makes a sound.
- `on_leave`: no train until you leave. Only line problems are told before that. When you leave home (or work on the way back), tap I'm leaving, or reach a station, TCC follows the next train you can catch. If nothing happens, the window closes quietly. Without a location block this works like `earliest_silent`.

`alerts.quiet_repeat_misses` still applies in every mode.

## Minimum stay

```yaml
return:
  mode: window
  window: ["15:00", "20:00"]
  min_stay_min: 480
  buffer_min: 5
```

`min_stay_min` keeps a window day from starting too soon after you arrived. The day starts at the later of the window start and your arrival plus the stay. The arrival is the newest arrival on the other direction in the 24 hours before the window. TCC uses the first time your phone was at the destination, else the train arrival NS showed. An arrival you confirmed yourself wins over both.

With an arrival at 09:05 and 480 minutes, the return starts at 17:05. A start after the latest time is capped at the latest time.

TCC sends no NS request before the stay ends. If you leave early, it still follows a train: a leave of work, a station or an I'm leaving tap starts it.

`min_stay_min` works on window days only.

## Settings by day

`by_weekday` changes `mode`, `window`, `pick` and `min_stay_min` for single days, weekends included. A day without an entry uses the direction's own settings.

```yaml
return:
  first_train: "17:52"
  buffer_min: 5
  first_train_by_weekday:
    sat: "10:12"
  by_weekday:
    fri: {mode: window, window: ["13:00", "18:00"], min_stay_min: 300}
```

A window day in a fixed direction needs its own `window`. A fixed day in a window direction needs a first train: `first_train_by_weekday` for that day, or `first_train`. Its watch window is 60 minutes before to 120 minutes after that train, unless the day sets one. The first train per day stays in `first_train_by_weekday`, described below.

The Home Assistant profile editor can set all of this. See [Profile editor](profile-editor.md).

## Weekday departure times

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

Transfers, transfer minimums, buffers, active leads, advice preferences, and return day offsets stay shared. Use separate profiles if these settings differ by weekday. The Home Assistant profile editor can set or clear weekday departures without changing other profile settings.

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

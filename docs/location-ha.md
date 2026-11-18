# Location with Home Assistant

Phone location is not always reliable. In stations, tunnels, or with battery saving on, your position can be late or wrong. Location only helps the companion. It never hides a warning about your train.

Location is off by default. A profile works fully on times alone.

## Turn it on

In the profile file, give the profile a `location` block:

```yaml
location:
  source: ha
  entity: person.someone
  zones:
    home: zone.home
    work: zone.work
  station_radius_m: 150
  wifi:                              # optional
    home: device_tracker.phone_wifi
  activity_entity: sensor.phone_activity  # optional
```

- `source: ha` reads the position from Home Assistant.
- `entity` is a `person` or a `device_tracker` that reports `latitude` and `longitude`. A tracker that only says `home` or `not_home` (a router, for example) has no coordinates and is ignored, so it can never cause a "left home" or a missed-train warning.
- `zones` maps the roles `home` and `work` to Home Assistant zone entities. `home` falls back to `zone.home`. A role whose zone does not exist is skipped.
- `station_radius_m` is the size of every station zone in metres. The default is 150.
- `wifi` (optional) maps the roles `home` and `work` to a tracker that is `home` while the phone is on that place's Wi-Fi, for example a UniFi or router `device_tracker`. Such trackers say `not_home` a few minutes late, which is why a connected Wi-Fi proves a miss only when your `buffer_min` is longer than 5 minutes. The Companion app's SSID sensor is not a tracker and does not work here.
- `activity_entity` (optional) is the phone's motion activity sensor, for example the Companion app's activity sensor on iOS (`Stationary`, `Walking`, `Automotive`) or on Android (`still`, `on_foot`, `in_vehicle`). It needs motion access, see [Phone setup](#phone-setup).
- The speed of a fix is read from the tracker's `speed` attribute (metres per second), or for a `person` from its source tracker. Nothing needs to be set for it.

## Phone setup

Allow background location so the companion can use your phone's position. Motion is optional. Without it, a miss needs Wi-Fi or the 12-minute check, and that check still needs a valid tracker position at home/work or at the boarding station.

- **Location.** iOS: Settings, Privacy & Security, Location Services, Home Assistant: Always, with Precise Location on. Android: Location, Allow all the time. On iOS, background sensor updates ride on location updates, so this also keeps the activity sensor fresh.
- **Motion on iOS.** The app does not ask by itself, so Home Assistant is missing from Settings, Privacy & Security, Motion & Fitness. In the Home Assistant app open Settings, Companion App, Sensors. Tap the blue **Motion** link above the sensor list and allow access. Then enable **Activity**. The sensor is usually `sensor.<phone>_activity`.
- **Motion on Android.** Turn on the Detected activity sensor (Settings, Companion app, Manage sensors) and allow Physical activity. It exists only in the Play Store version of the app. The sensor is usually `sensor.<phone>_detected_activity`.
- **Check it.** In Home Assistant, Developer tools, States: the phone tracker has `latitude` and `longitude` attributes. If you enabled motion, the activity sensor is not `unavailable`. Its first value can be `Unknown` until you move.

The phone reports activity when the operating system decides to, not every minute. That is why activity only adds proof and is never needed.

## Stations

Station zones are not set up by hand. The companion takes each station's position from NS coordinates and makes a zone around the home station, the work station, every transfer station, and every station of a journey that is still being followed (an old route, or a reroute through a new transfer station, keeps its zones until it ends).

## What it does

Leaving a zone, entering one, and reports from inside a station zone are sent as they happen. Reports from one zone are sent at most once every 2 minutes, because each one costs a live NS request. A position inside those 2 minutes is dropped, not kept for later: if your phone then stops moving, a missed-train warning waits for its next changed position. An update NS could not answer does not start those 2 minutes, so the next position is sent at once. A state that is `unavailable` or `unknown`, has no coordinates, or repeats the previous coordinates is ignored. A `gps_accuracy` larger than the station radius cannot place you in a zone, so it is ignored too, unless even its whole accuracy circle lies outside every zone: then it counts as leaving the zone you were in. Starting Home Assistant inside a zone sends nothing. A phone at rest sends no updates, so the companion tells itself that the tracker still places you somewhere, once per train and step, again when NS moves the train later:

- About 5 minutes before your locked first train leaves (half your `buffer_min` when that is shorter, so a 2-minute buffer checks 1 minute before), if you are still in the home zone (the work zone on the way back). This says only where you are; it never proves a miss.
- When that train leaves, if that place's Wi-Fi tracker is still connected.
- 12 minutes after a locked train left, if you are still at its boarding station (or, for the first train, still at home or work). One that NS could not answer is tried again while that step lasts.

Leaving home before your usual train leaves picks the next train you can still catch. A "left home" that comes only after your usual train left does not change your train, because a phone often reports late. It also counts as possibly riding until you enter the station, where a miss then needs the proof below; the silent note names the next good train.

## Missed trains

A phone often reports a position minutes late, even while you are already on the train. So a position at your boarding station, or at home (work on the way back), after the train left only makes a miss possible. The companion says "you missed" only with proof:

- Riding wins: any of these after the departure keeps following the train and sends nothing. A position outside that station or place (also a coarse cell-level one far away), a speed of 8 m/s or more, or an `Automotive` activity reported in the last 5 minutes. Entering that station or place again afterwards cancels a position outside it, for example a GPS jump over the zone's edge.
- Missed at once: when the train leaves, your home Wi-Fi is still connected and your `buffer_min` is longer than 5 minutes. The work Wi-Fi works the same way on the way back.
- Missed at once: 3 minutes or more after the departure, your phone reports `Stationary` or `Walking` (reported after the train left, at most 5 minutes ago) while it is still in that station or place.
- Missed after waiting: 12 minutes after the departure the tracker still places you there and nothing said you were riding.

A miss confirmed at home (at work on the way back) says when to leave, for example "you missed the 07:25. Next you can catch: 07:55. Leave by 07:35." "Leave by" is that train's departure minus `buffer_min`. When no train can still be caught, the same message says so. A confirmed missed train is dropped: you get no more messages about it.

Without proof you get the usual silent "has left. If you are not on it, next good connection: ..." note, and the train is still followed in case you are on it. If you were at the boarding station before the train left, that note is skipped. Leaving the station again before the train leaves, or arriving there only after it left, keeps the note.

A train followed this way that you were never seen boarding ends quietly. If no position ever put you at the station where it, or the journey, starts, its arrival says nothing about you: you get no "I stopped following" message, and the companion follows no train until you reach the station, leave your origin or choose a train. If none of that happens, the window closes without a message too.

With no Wi-Fi or activity entity set, misses come only from waiting 12 minutes, and riding is seen from positions and speed. A phone left at home, or a tracker stuck at the station while you ride, can still give a wrong "you missed" message 12 minutes after the departure. After a Home Assistant restart, a step still due may be sent once more, and an activity reported in the first 5 minutes is ignored, because Home Assistant stamps every restored state with its start time.

Arriving at the station where your journey starts sends one quiet message with the train now followed, its platform, whether it is on time and when it arrives. If that arrival shows you missed your train, the miss and the next train come in one message. Leaving and re-entering the zone does not repeat it.

Location only adds alerts. It can make a warning arrive sooner, or tell the companion you are still at a station after your train left. It never removes or delays a warning that times alone would give. The exceptions are the silent "has left, if you are not on it" note, which is skipped when you were at the boarding station, and replaced by the missed message when Wi-Fi already proved the miss, and the "I stopped following" message for a train you were never seen boarding.

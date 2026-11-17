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
```

- `source: ha` reads the position from Home Assistant.
- `entity` is a `person` or a `device_tracker` that reports `latitude` and `longitude`. A tracker that only says `home` or `not_home` (a router, for example) has no coordinates and is ignored, so it can never cause a "left home" or a missed-train warning.
- `zones` maps the roles `home` and `work` to Home Assistant zone entities. `home` falls back to `zone.home`. A role whose zone does not exist is skipped.
- `station_radius_m` is the size of every station zone in metres. The default is 150.

## Stations

Station zones are not set up by hand. The companion takes each station's position from NS coordinates and makes a zone around the home station, the work station, every transfer station, and every station of a journey that is still being followed (an old route, or a reroute through a new transfer station, keeps its zones until it ends).

## What it does

Leaving a zone, entering one, and reports from inside a station zone are sent as they happen. Reports from one zone are sent at most once every 2 minutes, because each one costs a live NS request. A position inside those 2 minutes is dropped, not kept for later: if your phone then stops moving, a missed-train warning waits for its next changed position. An update NS could not answer does not start those 2 minutes, so the next position is sent at once. A state that is `unavailable` or `unknown`, has no coordinates, repeats the previous coordinates, or has a `gps_accuracy` larger than the station radius is ignored. Starting Home Assistant inside a zone sends nothing. A phone at rest at home sends no updates, so about 5 minutes before your locked first train leaves (half your `buffer_min` when that is shorter, so a 2-minute buffer checks 1 minute before), if the tracker still places you in the home zone (the work zone on the way back), the companion tells itself that you are still there. It does so once per departure time, so again when NS moves the train later.

Leaving home before your usual train leaves picks the next train you can still catch. A "left home" that comes only after your usual train left does not change your train, because a phone often reports late. If you really missed it, arriving at the station picks the next good train.

A report from a station counts as a missed train only from 3 minutes after its expected departure; before that the train may still be pulling out. A missed train is dropped: you get no more messages about it. If you were at the boarding station before the train left, you get no "has left" note. Leaving the station again before the train leaves, or arriving there only after it left, keeps that note.

If you are still at home (at work on the way back) when your first train leaves, too late to reach it with your `buffer_min`, the companion marks it missed and sends one loud message, for example "you missed the 07:25. Next you can catch: 07:55. Leave by 07:35." "Leave by" is that train's departure minus `buffer_min`. When no train can still be caught, the same message says so. Leaving home before the train leaves prevents this. A phone left at home, or a "left home" that arrives late, can give a wrong "you missed" message.

Arriving at the station where your journey starts sends one quiet message with the train now followed, its platform, whether it is on time and when it arrives. If that arrival shows you missed your train, the miss and the next train come in one message. Leaving and re-entering the zone does not repeat it.

Location only adds alerts. It can make a warning arrive sooner, or tell the companion you are still at a station after your train left. It never removes or delays a warning that times alone would give. The one exception is the silent "has left, if you are not on it" note, which is skipped when you were at the boarding station, and replaced by the missed message when you were still at home.

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

Leaving a zone, entering one, and reports from inside a station zone are sent as they happen. Reports from one zone are sent at most once every 2 minutes, because each one costs a live NS request. A state that is `unavailable` or `unknown`, has no coordinates, repeats the previous coordinates, or has a `gps_accuracy` larger than the station radius is ignored. Starting Home Assistant inside a zone sends nothing.

Location only adds alerts. It can make a warning arrive sooner, or tell the companion you are still at a station after your train left. It never removes or delays a warning that times alone would give.

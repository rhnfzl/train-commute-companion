# Location with OwnTracks

Phone location is not always reliable. In stations, tunnels, or with battery saving on, your position can be late or wrong. Location only helps the companion. It never hides a warning about your train.

Location is off by default. A profile works fully on times alone. OwnTracks runs in the background on your phone and, through its regions, can tell the companion that you left home or reached work, which Telegram live location cannot. It takes longer to set up.

## The endpoint

Set `OWNTRACKS_SECRET` in `.env` and apply it with `docker compose up -d --force-recreate tcc` (a plain `restart` keeps the old environment). Without it, `/owntracks` answers 404. The endpoint is `POST http://<host>:8080/owntracks` with HTTP Basic authentication: any user name, and the secret as the password.

The phone must reach this endpoint from mobile data during the commute, for example through a VPN to your home network, or through a reverse proxy with TLS. An endpoint that is only reachable on your home network gets the queued positions after the commute, when they are too old to use, so it is for testing only. A plain HTTP endpoint sends the secret in clear: use TLS or a VPN outside your home network.

Expose only `/owntracks`. Forward or proxy that one path, never `/health`, whose body lists your journeys.

## The phone

In the OwnTracks app, open the connection settings:

- Mode: HTTP.
- URL: `http://<host>:8080/owntracks` (or your proxy's `https://` address for that path).
- Authentication: on, any user name, the `OWNTRACKS_SECRET` as the password.

Then add two regions in OwnTracks: one around your home and one around your work. Name them exactly like the profile's `zones.home` and `zones.work` values (by default `home` and `work`).

## The profile

```yaml
location:
  source: owntracks
  zones:
    home: home
    work: work
  station_radius_m: 150
```

- `source: owntracks` reads the position from the OwnTracks endpoint.
- `zones` gives the OwnTracks region names for the roles `home` and `work`. Leave it out to use the names `home` and `work`.
- `station_radius_m` is the size of every station zone in metres. The default is 150.

## Stations

Station zones are not set up by hand. The companion takes each station's position from NS coordinates and makes a zone around the home station, the work station, every transfer station, and every station of a journey that is still being followed. A station zone wins over a home or work region when you are inside both.

## What it does

Entering a zone, leaving one, and updates from inside one are sent as they happen. Updates from one zone are sent at most once every 2 minutes, because each one costs a live NS request. The first position after the endpoint starts (or after the profile's location settings change) only tells the companion where you are; it sends nothing. A position older than the last one used, more than 10 minutes old, more than 2 minutes in the future, or less accurate than the station radius is ignored. Other OwnTracks messages (region transitions, waypoints, last will) are answered and ignored.

Location only adds alerts. It can make a warning arrive sooner, or tell the companion you are still at a station after your train left. It never removes or delays a warning that times alone would give.

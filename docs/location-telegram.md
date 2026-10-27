# Location with Telegram

Phone location is not always reliable. In stations, tunnels, or with battery saving on, your position can be late or wrong. Location only helps the companion. It never hides a warning about your train.

Location is off by default. A profile works fully on times alone. Telegram live location needs no extra app, but it only sees stations: it cannot tell that you left home or reached work. For that, use [OwnTracks](location-owntracks.md) instead.

## Turn it on

In the profile file, give the profile a `location` block:

```yaml
location:
  source: telegram
  station_radius_m: 150
```

- `source: telegram` reads the position from the live location you share with the bot.
- `station_radius_m` is the size of every station zone in metres. The default is 150.

Then, in the chat with your bot, tap the attachment button, pick Location, then "Share My Live Location", and choose "until I turn it off". A live location with a time limit stops by itself, and the companion falls back to times alone. Only the chat set in `TELEGRAM_CHAT_ID` is read; a location from any other chat is ignored.

## Stations

Station zones are not set up by hand. The companion takes each station's position from NS coordinates and makes a zone around the home station, the work station, every transfer station, and every station of a journey that is still being followed. Home and work are only detected as stations: leaving your house is not.

## What it does

Entering a station zone, leaving one, and updates from inside one are sent as they happen. Updates from one zone are sent at most once every 2 minutes, because each one costs a live NS request. The first position after you start sharing (or after the profile's location settings change) only tells the companion where you are; it sends nothing. A position older than the last one used, more than 10 minutes old, more than 2 minutes in the future, or less accurate than the station radius is ignored.

Location only adds alerts. It can make a warning arrive sooner, or tell the companion you are still at a station after your train left. It never removes or delays a warning that times alone would give.

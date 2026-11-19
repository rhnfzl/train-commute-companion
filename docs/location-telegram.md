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

Then, in the chat with your bot, tap the attachment button, pick Location, then "Share My Live Location", and choose "until I turn it off". A live location with a time limit stops by itself, and the companion falls back to times alone. Only the chat set in `TELEGRAM_CHAT_ID` is read; a location from any other chat is ignored. That chat is one person's, so every position goes to every profile with `source: telegram`. For a second person, run a second copy with its own bot.

## Stations

Station zones are not set up by hand. The companion takes each station's position from NS coordinates and makes a zone around the home station, the work station, every transfer station, and every station of a journey that is still being followed. Home and work are only detected as stations: leaving your house is not.

## What it does

Entering a station zone, leaving one, and updates from inside one are sent as they happen. Updates from one zone are sent at most once every 2 minutes, because each one costs a live NS request. An update NS could not answer does not start those 2 minutes, so the next position is sent at once. The first position after you start sharing (or after the profile's location settings change) only tells the companion where you are; it sends nothing. A position older than the last one used, more than 10 minutes old, more than 2 minutes in the future, or less accurate than the station radius is ignored, unless even its whole accuracy circle lies outside every station zone: then it counts as leaving the zone you were in.

A phone often reports a position minutes late, even on the train, so a report from your boarding station after the train left only makes a miss possible. A miss is confirmed when you are still there 12 minutes after the departure and no position since placed you outside that station; any position outside it means you may be on the train, and it is followed. Telegram live location gives no speed, Wi-Fi or motion activity, so riding is seen only from positions, and a phone at rest that sends no update leaves the miss unconfirmed: you then get only the silent "has left, if you are not on it" note. The Home Assistant source can also use Wi-Fi, motion activity and speed to decide sooner. A confirmed missed train is dropped: you get no more messages about it. If you were at the boarding station before the train left, you get no "has left" note. Leaving the station again before the train leaves, or arriving there only after it left, keeps that note.

Arriving at the station where your journey starts sends one quiet message with the train now followed, its platform, whether it is on time and when it arrives. If that arrival shows you missed your train, the miss and the next train come in one message. Leaving and re-entering the zone does not repeat it.

Location only adds alerts. It can make a warning arrive sooner, or tell the companion you are still at a station after your train left. It never removes or delays a warning that times alone would give. The one exception is the silent "has left, if you are not on it" note, which is skipped when you were at the boarding station.

## Other boarding station

Boarding somewhere other than your usual station is noticed from your position. See [Other boarding station](other-boarding-station.md). Near such a station, while no train is followed, a fix counts when its accuracy is within `other_station_radius_m` (default 500 m), not only within the station radius. Telegram live location sends no speed or motion activity, so driving past a station cannot be told apart. Set `other_station_dwell_min` above 0 if passing stations causes questions.

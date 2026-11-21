# Other boarding station

Sometimes you board somewhere else. For example, you take the train at Den Haag HS instead of your usual station. With location on, the companion notices this and follows your train from there.

It needs a location source: [Home Assistant](location-ha.md), [OwnTracks](location-owntracks.md) or [Telegram live location](location-telegram.md).

## When it looks

Only while a trip is open and no train is followed yet. That is before the first train is chosen, or after a train you never boarded was let go. A train TCC guessed by itself, not yet left and not yet confirmed at a station, also counts as not followed.

These do not count:

- Driving past. A fix at 8 m/s or faster, or a fresh in-vehicle motion activity, is ignored. Home Assistant sends speed and activity. OwnTracks sends its speed (`vel`). For Telegram, and OwnTracks without a speed, the speed between two fixes is used. If passing stations still causes questions, set `other_station_dwell_min` above 0.
- A station stop while you ride a followed train.
- A station within 30 minutes of being at another one. That is a train you ride but TCC does not follow, stopping on the way. You get at most one question for it.
- A fix inside your home or work zone.
- The station where this direction starts or ends.
- A fix whose accuracy is worse than the distance setting, or a fix with no accuracy.
- A fix that is nearer to the station where this direction starts or ends than to any other station.

## Settings

```yaml
location:
  source: ha
  entity: person.me
  other_station_radius_m: 500     # how near the phone must be to the station point
  other_station_dwell_min: 0      # 0: one fix is enough
  ask_to_remember_station: true   # after Yes at a new station, ask to remember it
return:
  first_train: "17:36"
  buffer_min: 2
  other_stations: [GV]            # follow a train from these by itself
```

The defaults come from real trips. At a usual station the phone was 40 to 115 m from the station point, often with only one or two fixes before boarding. At another station the only fix was about 400 m away, 16 minutes before the train left. Set a smaller distance if a station near your walk causes a question you do not want.

The [profile editor](profile-editor.md) has the same settings.

## Known stations

A station in `other_stations` is followed by itself when no train is followed yet. The companion follows the next train you can catch there to the normal destination of that direction. You get one quiet message that names the station and the train.

This first fix may be up to 500 m away, for example from a cafe near the station. So the train is only a guess until a fix inside the usual station distance (`station_radius_m`). Until then, messages about it, a cancellation included, are quiet. If you never come that close, the train is let go quietly, like any train you were never seen boarding. You get no "you missed" message for it.

While TCC still holds its own guess of a train from your usual station, a known station is asked about instead, with the same buttons as below.

## Other stations

At any other station you get one quiet question: "are you taking the 20:06 from Den Haag HS to Rotterdam Centraal?" with three buttons.

- **Yes**: that train is followed from that station.
- **Other train**: you get the next trains from there as buttons. Tap one to follow it. Until you tap one, nothing changes.
- **No**: nothing happens.

No answer means nothing happens. You are asked at most once per station, per direction, per day. A transfer station of your route counts as an other station while no train you follow runs through it.

Nothing changes until NS answers. If NS does not answer, the question stays open, so you can tap again. A question expires when you turn up at your usual station, or when a train from elsewhere is already confirmed.

After Yes, a second question asks whether to remember the station. No answer means it is not remembered. You can still answer it after you arrived, until the trip's window closes. Yes adds it to a list in the companion's own state. Your profile file is never changed. A remembered station is then followed by itself, like one in `other_stations`. Turn this question off with `ask_to_remember_station: false`.

See and remove remembered stations:

- Home Assistant: the `remembered_stations` attribute of the status sensor, and the `forget_station` service.
- Docker: `/stations` and `/forget STATION`.

## Missed trains there

The missed-train rules still apply at the other station. Still being there 12 minutes after your train left, or a fresh at-rest activity after it left, proves a miss there. Your home or work Wi-Fi cannot prove a miss at another station.

Once a station is your boarding station, it uses the usual station distance (`station_radius_m`), not the wider one. A fix 400 m away then proves neither boarding nor a miss.

# Commute statistics

See how often your trains arrived on time and which weekday had the highest average delay. The companion adds a row after it finishes monitoring a journey. Use the private Today page or send `/stats week`, `/stats month`, or `/stats year` in your confirmed Telegram chat.

Those periods cover the last 7, 30, or 365 calendar days, including today. An NS arrival less than three minutes after the saved plan counts as on time. Early arrivals contribute zero to the average delay. An unknown arrival does not count in the on-time rate or average. The summary also shows trains that were cancelled or cut short, changed, or missed. An NS train arrival does not show when a traveller reached their final destination.

Home Assistant exposes on-time rate and average delay as 30-day measurement sensors per profile. Home Assistant can store the sensor measurements for its graphs.

## Profile settings

The three switches appear in the Home Assistant profile editor. In YAML, set them under `alerts`.

~~~yaml
alerts:
  commute_stats: true
  weekly_recap: true
  after_trip_note: true
~~~

- **Record commute statistics** defaults to on. The companion keeps up to 400 calendar days of rows locally. Rows contain the journey date, direction, planned times, NS arrival, and journey-change flags. They contain no coordinates or phone location.
- **Send a silent weekly recap** defaults to on. The companion sends at most one recap per profile each Sunday after 19:00 in the Netherlands, when that week has recorded journeys.
- **Send a silent note after a journey** defaults to on. The companion sends the NS arrival note when it finishes monitoring on the arrival day.

Turning off statistics stops recording and hides that profile's metrics. It also disables both notifications. The reminders use the profile's routine notification channels and quiet-hour settings. Disable a reminder by itself to keep the statistics without that message.

## Delete local statistics

In Telegram, send `/clearstats` and review the number of rows before confirming. In Home Assistant, an administrator can run `train_commute_companion.clear_commute_history` with `confirm: true`.

This removes all local commute statistics rows and queued recap and trip notes. It preserves saved compensation records. Home Assistant's recorder and messages already received by a delivery service follow their own retention rules.

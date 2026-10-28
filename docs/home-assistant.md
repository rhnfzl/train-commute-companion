# Train Commute Companion for Home Assistant

Data: NS. You bring your own NS API key; none is bundled. This integration follows your usual NS train journey and tells you on your phone when something changes: a delay, a cancelled train, a broken transfer, a platform change.

It needs Home Assistant 2026.10 or newer and the Home Assistant companion app on your phone.

## Install

1. In HACS, open the menu, choose Custom repositories, and add `https://github.com/OWNER/train-commute-companion` as an Integration.
2. Download Train Commute Companion in HACS and restart Home Assistant.

## Set up

1. Get an NS API key from the NS API portal.
2. Copy `examples/profiles.yaml` from the repository to `<config>/train_commute_companion/profiles.yaml`. Replace the placeholder station `AAA` with your own NS station codes, and set your own first trains, transfer stations and days. Quote every time (`"08:12"`).
3. Go to Settings, Devices and services, Add integration, Train Commute Companion. Enter the key and the path of the file. If you keep the file outside the config folder, allow its folder in `allowlist_external_dirs`.
4. Pick your phone and the loud alert style (below). Two test notifications go out, one loud and one silent. Confirm that both arrived.

Only one NS key is set up per Home Assistant instance.

### Loud alert style

A loud alert is one you must not miss. Quiet updates are always silent. You choose how a loud alert arrives, in the setup and later under Configure, Change settings:

- Time-sensitive (the default): breaks through Focus modes. A phone that is muted stays silent.
- Critical: sounds on a muted phone and in Focus.

For critical to work, set up your phone. The menu names below follow the companion app as we know it and have not been checked on a device, so they may differ on your phone or app version:

- iOS: allow Critical Alerts for the Home Assistant companion app (Settings, Notifications, Home Assistant, Critical Alerts).
- Android: a critical alert uses the `alarm_stream` channel, which plays at alarm volume even in silent or vibrate mode. To also pass Do Not Disturb, open the channel's settings (Settings, Apps, Home Assistant, Notifications, `alarm_stream`) and turn on "Override Do Not Disturb". Then give the companion app Do Not Disturb access (Settings, Apps, Special app access, Do Not Disturb access, Home Assistant).

When you change the alert style or the notify target (Configure, Change settings, then save), the integration sends a loud and a silent test. Turn Do Not Disturb on first and check that the loud test still rings. Saving without changing either sends no test.

### Check the file

Under Configure, Check the profiles file, the integration reads your file and names every error, with a suggestion where it can. Run it after every edit. The same file works with `tcc check-profile`.

The file is checked again about every 10 seconds. A valid edit becomes the active configuration without a restart. If an edit has an error, the whole edit is refused, the last valid configuration keeps running, and you get one loud message and a repair issue that goes away when the file loads. If the file is missing or broken when Home Assistant starts, the last valid configuration runs and you get the same refusal.

## What you get

Per profile and direction (`outbound`, `return`):

- Sensors: status, lock (the trains you are following), next connection, earlier train advice, works ahead, one sensor per leg and one per transfer.
- Buttons: leaving, not travelling, choose next.

Per profile: a health sensor and a "not travelling today" button.

Entity IDs come from the profile `name`, for example `sensor.office_a_outbound_status`. Sensor states are plain text; a value that does not apply is `none`.

### Services

All three take `profile` (the profile `id` or `name`) and optionally `direction` and `service_date`.

- `train_commute_companion.leaving`: you are on your way.
- `train_commute_companion.not_travelling`: you are not travelling (`whole_day: true` for both directions).
- `train_commute_companion.choose`: name the train you will take by its `departure` time, with an optional `train_number`.

A service or button used when there is nothing to act on, or with an unknown profile, fails with a message that says why.

### Event

Every alert fires the event `train_commute_companion_alert` before the push, so automations see it even if the phone cannot be reached. Fields: `config_entry_id`, `device_id`, `profile_id`, `profile_name`, `direction`, `service_date`, `leg_index`, `kind`, `severity` (`loud` or `silent`), `train_operating_date`, `train_number`, `train_departure`, `text`, `key`, `created_at`.

When copies of your train in one NS answer disagree, for example on a time or a platform, you get one loud alert with `kind` `data_unclear` that shows each copy's values; check the NS app or the station boards. It comes once per train per day, again only when the values change; the look-ahead may also tell it once ahead of the day.

### Repairs

- Profile file refused: your last edit or start-up file was not used.
- NS moved your train: NS has moved your usual train; the integration follows the new time and never rewrites your file.
- Alerts are not reaching your phone: calls to the notify service fail. Check the app or pick another phone in the options.

## Webhooks

Webhooks are optional. Under Configure, Webhooks, enter one URL per line (for example `http://localhost:8644/webhooks/commute`), a secret of at least 16 characters, and a bearer token only if your receiver wants one. Every alert is then also posted as signed JSON to each URL; [Webhooks](webhooks.md) has the payload, the headers and how to verify them. Leave the URLs empty to turn webhooks off.

Saving a changed URL, secret or token first sends each URL a signed loud and a signed silent test. If any URL does not accept both, nothing is saved, the form names the host and both answers, and the old settings keep running. Saving without a change sends no test. Your phone stays the proof that alerts arrive.

The payload carries your profile names, stations and times. Use a receiver on this machine where you can, and https for any receiver off it.

A receiver that fails or is down never holds back a phone alert. It shows only as a warning in the Home Assistant log, never as a repair issue or a sensor.

## Limits

- NS trip data reaches about 10 days ahead, so the look-ahead for planned works does too. A day further out is not checked yet, rather than looking falsely clean.
- A machine that is off cannot warn you about itself. If Home Assistant is down, no alert arrives. Add an external uptime check for the machine that runs it.
- Delivery means Home Assistant handed the push on and saw no failure. What the phone vendor's relay does after that is not visible to the integration.

## Location

Location is optional and only adds alerts. See [Location with Home Assistant](location-ha.md).

Phone location is not always reliable. In stations, tunnels, or with battery saving on, your position can be late or wrong. Location only helps the companion. It never hides a warning about your train.

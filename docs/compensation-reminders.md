# Confirm an arrival and check NS compensation rules

You can save your actual destination arrival and compare it with your original plan.
The companion links to NS rules. It does not decide eligibility or submit a claim.

NS conditions depend on your ticket, route, operator and other circumstances.
Planned works use adjusted timetables. A difference from your usual commute does not prove entitlement.
Read the [current NS rules](https://www.ns.nl/service-en-contact/geld-terug/geld-terug-bij-vertraging) before submitting a claim.

## Save an arrival

In Telegram, send `/arrived` to see the input format and saved journey references.
Enter the profile ID, direction, service date, exact destination code and actual arrival timestamp:

```text
/arrived office outbound 2026-11-06 RTD 2026-11-06T09:40+01:00
```

Use a full date and timezone offset. Dutch summer time normally uses `+02:00`; winter time uses `+01:00`.
Review the destination, service date, actual arrival and original planned arrival. Then press the confirmation button.
The button records your entered time. It never substitutes the current time.
A review expires after ten minutes and after a bot restart.
Foreign chats cannot inspect or confirm it.

When no original plan was saved, enter your original planned arrival explicitly:

```text
/arrived office outbound 2026-11-06 RTD 2026-11-06T09:40+01:00 planned=2026-11-06T09:00+01:00
```

The reference then shows `user` as its source. It does not become an NS timetable observation.
Send `/arrived` again with corrected actual details to correct a saved arrival.
An existing original plan remains fixed.
A stale review refuses to overwrite a newer correction.
A failed storage write refuses the action. It cannot activate later without another explicit confirmation.

Telegram delivery confirmation is required before Telegram accepts arrival actions.

### Home Assistant

Open Configure > Arrival on the integration. Choose the saved journey, confirm its destination and enter your actual arrival with the date/time picker.
The final review shows the canonical Amsterdam time before saving. It does not record the current time automatically.
Repeated autumn clock-change times require an explicit offset choice. Nonexistent spring times are refused.
Enter the original planned arrival when no trustworthy reference exists. Earlier saved arrivals can be corrected through the same form.

Enable automatic reminders in the profile details form. The setting is off by default.
Arrival actions do not edit profiles or reload the integration.

Automations can call `confirm_arrival` with a saved profile name or immutable ID.
Include direction, service date, exact destination, aware ISO timestamp, expected revision and action ID.
Archived journeys remain selectable while their records are retained. A deleted profile requires its immutable ID.
Service responses contain the saved record and the official NS guidance link. A failed save returns an error.

## Optional reminder

Reminders are off by default. Enable them for a profile:

```yaml
alerts:
  compensation_reminder: true
```

A confirmed difference of at least 30 minutes can create one reminder to check NS rules.
This threshold triggers a saved-plan comparison only. It is not an eligibility rule.
Reminders use the existing `routine` notification category, quiet hours and channel routing.
Disabling `routine` suppresses the reminder. Direct arrival feedback still includes the official NS link.
A reminder expires seven days after it is created.
This expiry is a product rule, not an NS claim deadline.

A journey can create at most one reminder across corrections and restarts.
A correction invalidates an unsent reminder. It does not retract a message already accepted by a recipient.
Later corrections still return the current comparison and official link, without automatic repeat reminders.

## What is retained

The first trustworthy selected plan stores its explicit planned departure, final planned arrival and destination.
It also records the NS answer time when available.
The reference freezes before automatic train replacement or manual multimodal recovery.
A missing usual train's fallback does not become an original reference.
Existing state files without an original reference require explicit user input.

Records remain available after the active monitoring occurrence is archived.
Plan records are saved even when automatic reminders are disabled. Turning reminders off does not erase those records.
They remain for 120 days after the service date, or longer while a pending delivery still references them.
This retention period is a product rule. It does not extend the NS claim period.
Provider forecasts, elapsed time, location reports and inferred train progress never confirm actual arrival.

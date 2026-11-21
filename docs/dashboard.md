# Home Assistant dashboard

See your followed trains, transfers and next connection beside the buttons you use when leaving or skipping a journey. The example uses standard Home Assistant cards, so it needs no extra frontend integration.

## Add the card

1. Set up the integration and confirm both test notifications arrive.
2. Open **Developer Tools > Actions** and select **Build a commute dashboard card**.
3. Enter your profile name or ID. Choose **Outbound** or **Return** and run the action.
4. Copy the response. Open your dashboard and select **Edit dashboard > Add card > Manual**.
5. Paste the response and save. Repeat for another direction or profile.

You can also run the [example action in YAML mode](../examples/dashboard.yaml). The action uses
the entity IDs already registered in Home Assistant. No changes to the response are needed.
If you rename the health, status, next connection, advice, works or button entities later,
generate the card again. The train and transfer lists follow the profile's device automatically.

## What it shows

- The journey state and each followed train's departure, arrival, platform and delay.
- Transfer state, available minutes and the required minimum.
- The next catchable connection and earlier-train advice.
- Planned-works dates within the checked horizon.
- Data health and the last successful NS answer and works check.
- Leaving, choose next, skip outbound and skip today buttons.

The lists include extra legs or transfers when the integration adds them during recovery. `Unknown` means a value is missing. A time marked `planned` has no reported live time. The health warning remains visible when current journey data is not verified. Check the NS app or station board before acting on stored times.

The works count only covers checked commute dates, not every future journey. Its last-check time helps you recognise an old result. Buttons show an error if there is no journey or connection they can act on.

The direction's status sensor exposes `leave_by` as an ISO timestamp with a UTC offset for the followed first train.
It uses the earlier of planned and NS-expected departure, minus the journey's travel buffer.
The attribute is null when no train is followed or `alerts.leave_by` is false.
`weather_allowance` holds suggested extra minutes separately from this timestamp. `leave_by_text` displays the applicable advice.
See the [weather allowance table](weather.md#suggested-extra-time-before-departure). Suggestions never change timers or the followed train.

This dashboard contains your travel routine. Keep it on a private dashboard and remove station and journey details before sharing screenshots.

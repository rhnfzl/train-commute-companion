# Home Assistant dashboard

See your followed trains, transfers and next connection beside the buttons you use when leaving or skipping a journey. The example uses standard Home Assistant cards, so it needs no extra frontend integration.

## Add the card

1. Set up the integration and confirm both test notifications arrive.
2. Open Settings, Devices and services, Train Commute Companion, and inspect your profile's entities.
3. Open your dashboard, choose Edit dashboard, Add card, then Manual.
4. Paste [examples/dashboard.yaml](../examples/dashboard.yaml) into the card editor.
5. Replace every `office_a` with your actual entity prefix, then save.

The example targets the `outbound` direction. For a return card, copy it and replace `outbound` with `return`, including `direction`. Change the title to match. Keep the profile's health entity and whole-day skip button unchanged.

Use the entity IDs shown in Home Assistant. Renaming a profile does not necessarily rename entities already registered in Home Assistant. If you renamed individual entities, update their references too. The leg and transfer lists find entities by their shared prefix.

## What it shows

- The journey state and each followed train's departure, arrival, platform and delay.
- Transfer state, available minutes and the required minimum.
- The next catchable connection and earlier-train advice.
- Planned-works dates within the checked horizon.
- Data health and the last successful NS answer and works check.
- Leaving, choose next, skip outbound and skip today buttons.

The lists include extra legs or transfers when the integration adds them during recovery. `Unknown` means a value is missing. A time marked `planned` has no reported live time. The health warning remains visible when current journey data is not verified. Check the NS app or station board before acting on stored times.

The works count only covers checked commute dates, not every future journey. Its last-check time helps you recognise an old result. Buttons show an error if there is no journey or connection they can act on.

This dashboard contains your travel routine. Keep it on a private dashboard and remove station and journey details before sharing screenshots.

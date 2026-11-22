# Train Commute Companion for Android

This pilot shows your commute status and sets a wake alarm on this phone. The app checks your private companion while the status screen is open.

## Build

Use JDK 17 and Android SDK Platform 36. The app supports Android 9 and later. Android Studio can open this directory as a single project.

From this directory, set `ANDROID_HOME` to the SDK path or set `sdk.dir` in an untracked `local.properties`. Then run:

```sh
./gradlew :app:assembleDebug :app:testDebugUnitTest :app:lintDebug
```

The debug APK is at `app/build/outputs/apk/debug/app-debug.apk`. With a test handset connected over USB and Android platform tools available, install it with `adb install -r app/build/outputs/apk/debug/app-debug.apk`.

## Connect

1. Configure the companion’s [private MCP listener](../../docs/mcp.md#private-network-access) and a trusted HTTPS proxy.
2. Register a unique token for this phone. Set its write policy to **Deny** in Telegram’s `/agents` menu.
3. Connect the phone to the private network. Enter the HTTPS URL ending in `/mcp`, token name, bearer token and profile.
4. Tap **Connect** and check the journey snapshot and timetable times.

The app calls only `get_status` and uses Android’s default TLS trust. The token stays in memory and must be entered again after the app process exits. A successful timetable check is the time your companion checked NS.

## Wake alarm and trip notification

- **Set wake alarm** works with the connection off. Android asks for exact alarm access and allows the alarm controls through its notification settings. The app schedules one future wake alarm and uses the phone’s alarm sound and volume.
- **Set wake alarm for this trip** links the time you choose to that journey. The app can offer an earlier leave-by time for the same journey when the snapshot and last healthy timetable check are both under five minutes old. Confirm the change, then the app checks the same time again before it replaces the scheduled alarm.
- **Stop** ends the ringing. **Snooze 5 minutes** sets the next wake time before stopping the sound. The alarm rings for up to five minutes.
- **Follow trip on this phone** posts a standard Android notification for one explicit trip. It updates only when the app checks status in the foreground. It ends when a foreground check confirms the trip is no longer active, when you stop following, or after four hours. Android lock-screen privacy settings control which details are visible.

This pilot uses neither promoted Live Updates nor background trip sync. On restart, Android restores a future alarm after the first unlock if exact alarm access remains enabled. Forced stops, muted alarm volume, Do Not Disturb settings and vendor-specific restrictions can affect delivery.

## Handset checks

Complete these checks on each handset before relying on the pilot:

1. Connect to a private HTTPS endpoint using a dedicated token. Compare the three timestamps with the companion.
2. Reject notifications and exact alarm access separately. Confirm that the app explains what it needs.
3. Set an alarm a few minutes ahead. Lock the phone and disable its network. Check sound, **Snooze 5 minutes** and **Stop**.
4. Replace an alarm with a nearer time. Check that only the replacement sounds. Repeat with the app process killed normally and after reboot.
5. Review an earlier time. Disable the network before confirming. Check that the original alarm stays scheduled.
6. Follow a trip. Check the lock screen, then confirm that its timestamp stays fixed while the app is closed.

Platform references: [Exact alarms](https://developer.android.com/develop/background-work/services/alarms), [notification permission](https://developer.android.com/develop/ui/views/notifications/notification-permission), [foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types) and [Live Update eligibility](https://developer.android.com/develop/ui/views/notifications/live-update).

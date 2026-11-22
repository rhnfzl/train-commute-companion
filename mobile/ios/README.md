# iPhone pilot

Check your commute on an iPhone, review a wake time, and start a trip countdown on the Lock Screen.

## Build

Use Xcode 26.1 or later, XcodeGen 2.44 or later, and iOS 26 or later. Run these commands from this directory.

```sh
xcodegen generate
xcodebuild -project TrainCommute.xcodeproj -scheme TrainCommute \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

For an iPhone build, open the generated project in Xcode. Select your development team for both targets. Choose unique bundle identifiers with the widget identifier nested under the app identifier if required. Regeneration restores the signing settings in `project.yml`.

To run the portable checks with the Swift command line tools:

```sh
checks_dir="$(mktemp -d)"
swiftc -swift-version 6 Sources/CommuteCore/*.swift Tests/CommuteCoreTests/*.swift \
  -o "$checks_dir/commute-checks" &&
  "$checks_dir/commute-checks"
```

## Connect

Register a dedicated agent token on your companion. Use the operator's private HTTPS URL ending in `/mcp`. The iPhone must trust the TLS certificate. Enter the URL, profile, token name, and token in **Connection**. See the [companion MCP setup](../../docs/mcp.md#private-network-access).

The app calls only `get_status`. The relay can grant the registered token access to additional tools, so keep it private. The app stores the connection in the device-only Keychain.

## Verify on an iPhone

- Open the app on the private network. Confirm that the snapshot shows the correct profile, timetable check time, and available leave-by.
- Allow alarms. Set a manual alarm a few minutes ahead, lock the iPhone, and check the alert and five-minute snooze. Stop the snooze.
- Review a fresh timetable-based wake time. Disconnect from the private network before confirming. Check that any existing alarm stays scheduled.
- Reconnect and review an updated wake time. Confirm that only the replacement remains. Relaunch to verify that the app can identify it.
- Start a trip display. Check the arrival estimate and time-based progress on the Lock Screen and Dynamic Island, when available. End the display from the app.

The app refreshes when it opens and when you tap **Refresh commute**. Changing a timetable-based wake alarm requires your confirmation and another successful check. The app cannot adjust tomorrow's alarm while you sleep. An armed alarm keeps its scheduled time until you change it or snooze it. A trip display uses the last checked arrival estimate and marks it stale when it expires.

Apple references for the [alarm and snooze](https://developer.apple.com/documentation/alarmkit/scheduling-an-alarm-with-alarmkit) and [Live Activities](https://developer.apple.com/documentation/activitykit/displaying-live-data-with-live-activities).

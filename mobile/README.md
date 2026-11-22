# Native phone pilots

These pilot apps show your commute from your private Train Commute Companion and let you set a wake alarm on your phone. They use the Docker companion in release 0.9.0 or later. Your Home Assistant, Telegram, and ntfy alerts remain available.

## Connect to your companion

Follow the [private-network MCP setup](../docs/mcp.md#private-network-access). Use a trusted HTTPS proxy for the `/mcp` URL. Keep the proxy and the Docker listener private. Register a separate named agent for each phone and set its plan-change policy to **Deny**. Enter the URL, token, and profile ID in the app. Use the existing Home Assistant location and motion settings for the profile.

The pilots read `get_status` on demand. They show when the companion last checked the timetable. The phone can propose an earlier alarm from fresh evidence and asks you to confirm it. The app keeps the existing alarm if the connection fails or the evidence expires.

For a trip with a transfer, wake suggestions and trip following require transfer status from the companion. A companion without this field can still show the status and support these actions for direct trains.

## Test on a phone

- **iPhone.** Use full Xcode and an iPhone running iOS 26 or later. Enable Developer Mode and use automatic signing with your own team. Grant AlarmKit permission on the phone. [Build and test the iPhone app](ios/README.md)
- **Android.** Use JDK 17 and the Android SDK. Install the debug build on a test phone and allow exact alarms and notifications. [Build and test the Android app](android/README.md)

Test a short alarm while the phone is locked. Verify that it sounds and test stop and snooze. Then fail an update and confirm that the previous alarm still works. Check the displayed trip and its timestamp against the companion. Test each platform separately.

The pilots refresh while you use the app. The operating system owns an alarm after scheduling. These builds have yet to prove an automatic alarm change while the app is closed.

# Share a Docker schedule with Home Assistant

Keep regular commutes and flexible trips on the same Docker companion. Connect your existing Home Assistant integration so its devices, sensors, buttons and phone location use that schedule. The integration keeps its local mode unless you connect it to Docker.

## Prepare the connection

Use matching releases of the Docker companion and Home Assistant integration. Back up both instances and save a copy of the Home Assistant profiles file. Keep each profile's ID when copying it into the Docker profile file. The ID keeps existing Home Assistant entities associated with the same profile.

Set these private values in the Docker app environment.

| Setting | Value |
| --- | --- |
| `TCC_HOME_ASSISTANT_TOKEN` | A dedicated 64-digit lowercase hexadecimal string. Generate one with `openssl rand -hex 32`. |
| `TCC_HOME_ASSISTANT_PORT` | A separate HTTPS port inside the container, such as `8766`. |
| `TCC_HOME_ASSISTANT_TLS_CERT` | The mounted server certificate path. Its name or IP must match the address Home Assistant uses. |
| `TCC_HOME_ASSISTANT_TLS_KEY` | The mounted private key path. Give the companion user read access. |

Mount the certificate files read-only. Publish the HTTPS port only on a private interface that Home Assistant can reach. Keep the certificate authority outside the container and put a copy of its public certificate in Home Assistant's config folder if it uses a private CA.

## Move a regular commute

Choose a time when there are no journeys in progress. Start Docker with the private listener and its existing plans. In the existing Train Commute Companion integration, open **Configure**, then **Docker companion connection**. Enter the private HTTPS origin, the bridge token and, for a private CA, the certificate path.

Verify the integration switched to Docker before putting the regular commute profile into the Docker profiles file. That keeps one scheduler in charge throughout the move.

Verify the office sensors in Home Assistant and use `list_travel_plans` through MCP to see the regular commute alongside flexible trips. For automations that supply extra dates or station buffers, use `train_commute_companion.sync_schedule` to update the Docker profile. Keep the existing Home Assistant file as a backup.

To deliver selected profiles through Home Assistant's phone settings, also set `TCC_HOME_ASSISTANT_NOTIFY_URL` to the private HTTPS URL ending in `/api/train_commute_companion/bridge-alert`. List the profile IDs in `TCC_HOME_ASSISTANT_NOTIFY_PROFILES`. If the Home Assistant HTTPS endpoint uses a private CA, mount its public certificate and give its path in `TCC_HOME_ASSISTANT_NOTIFY_CA`.

Home Assistant applies the selected profiles' notification gates and fires its usual alert event. Telegram still handles the Docker companion's flexible trips and agent approvals. Use one Telegram delivery path for each profile. Set the phone location source to Home Assistant for profiles that use its location evidence.

Edit profiles, timetable settings, weather and webhooks on the Docker companion. Continue to use Home Assistant for its local phone settings and existing commute controls.

Check the first scheduled journey, the expected sensors and the phone notifications before removing any rollback files. If you need to return to the local runtime, remove the Docker copy of the regular profile from the running configuration before reconnecting the Home Assistant integration to its saved file.

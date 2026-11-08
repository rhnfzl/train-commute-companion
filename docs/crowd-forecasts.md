# Crowd forecasts

See NS crowd forecasts beside your followed trains and alternatives. A forecast describes expected crowding, not guaranteed seat availability.

The companion accepts NS values `LOW`, `MEDIUM` and `HIGH`. Missing or unsupported values stay unknown. Copies of the same train that disagree at the latest fetch also become unknown. A newer partial copy replaces an older crowd forecast, even when an older copy supplies the remaining journey details.

## Where forecasts appear

- Home Assistant leg attributes include `crowd_forecast`, with `low`, `medium`, `high` or `null`.
- The next-connection sensor includes the first train's forecast.
- The example dashboard labels missing forecasts `Unknown`.
- Telegram status and local MCP next-connection results include forecasts.
- On-time messages, delay alerts, earlier-train advice and replacement advice include available forecasts. Multi-leg advice names each leg and labels missing forecasts unknown.

## Crowd updates

Before departure, an initial high forecast sends a silent `crowd_change` alert. Initial low or medium forecasts establish a baseline without another alert. A later change to a known level sends a silent update.

Repeated forecasts do not repeat the alert. Delivered state survives restart, and failed sends use the existing retry handling. Unknown forecasts do not create a crowd update. Crowd updates stop after the train departs.

Held delay and replacement alerts refresh their crowd text before delivery. Unknown or stale evidence removes that optional text. Delay facts and warning severity remain unchanged.

Crowd forecasts do not override cancellation, transfer, catchability or reported-time checks. The companion does not select a slower train solely because it has a lower forecast.

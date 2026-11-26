# Weather for your commute

The companion shows weather near the departure and arrival stations at the times of your followed journey.
Short tips help you prepare for rain, snow, heat, frost and possible ice.
Forecast tips are estimates, not official warnings or proof of conditions on a path or platform.

Weather is on by default. Turn it off in the Home Assistant profile details form, or use YAML:

```yaml
alerts:
  weather: false
```

Off means no weather requests for that profile and no weather text.
Other profiles can still request the same station.
An existing request can finish after a settings change.
The command-line replay and fixture providers do not supply live weather.

## Forecasts without a key

Docker and Home Assistant use the [Open-Meteo KNMI forecast API](https://open-meteo.com/en/docs/knmi-api).
The request selects `knmi_seamless`, hourly Celsius temperature, precipitation in millimetres, snowfall in centimetres and WMO weather codes.
Only today and tomorrow are requested, within KNMI's model horizon.

Coordinates come from NS's loaded station catalogue. Phone coordinates and profile names never go to the weather service.
The closest hourly forecast within 30 minutes describes each endpoint's journey time.
Hourly precipitation describes the preceding hour, rather than exact rain during your walk.
Missing station coordinates, times or forecast values leave the forecast unavailable.

Tips use simple estimates: precipitation from 0.2 mm, any snowfall, temperature from 28°C, frost at 0°C or below, and freezing precipitation codes.
Precipitation at 1°C or below also prompts a possible-ice tip.
These thresholds do not predict actual surface conditions.

Telegram `/status`, MCP journey status and Home Assistant's status sensor attributes expose the same weather view.
Routine on-time and at-station messages use icons for weather tips and active warnings.
Identical advice at both stations appears once with both station names.
Open status for forecast values, timestamps, warning coverage and source links.
The initial message can precede the first weather response.
Weather adds no separate notification and does not change train selection, schedules or travel buffers.
Selected recovery journeys use the accepted itinerary's departure and arrival endpoints and times. An endpoint without a known station coordinate has no weather context. Street or phone coordinates are never substituted.

## Suggested extra time before departure

Leave-by text can suggest extra time for forecast rain, fog, snow or possible ice at the departure station.
For example: **Leave by 07:57 (+5 min suggested for forecast snow)** instead of the usual 08:02.
The direction's configured `buffer_min` determines the extra minutes:

| Travel buffer | Rain | Fog | Snow | Possible ice |
| --- | --- | --- | --- | --- |
| 0 minutes | 0 | 0 | 0 | 0 |
| 1 to 10 minutes | 2 | 2 | 5 | 5 |
| 11 to 20 minutes | 3 | 3 | 8 | 10 |
| 21 to 45 minutes | 5 | 5 | 10 | 15 |

This table is a planning heuristic, not a measured travel-time prediction or calibrated walking estimate.
The strongest applicable hazard wins: possible ice, then snow, then rain, then fog. Minutes never add together.
Rain uses at least 0.2 mm precipitation. Fog uses WMO codes 45 or 48.
Snow uses any snowfall or WMO codes 71, 73, 75, 77, 85 or 86.
Possible ice uses freezing precipitation codes 56, 57, 66 or 67, or precipitation from 0.2 mm at 1°C or below.
Dry frost alone adds no time. Hourly forecasts cannot establish the conditions along your walk.

The suggestion uses only a fresh cached departure-station forecast, within 30 minutes of that train's departure.
Missing, stale or malformed forecasts add no time. Arrival-station weather never adds time before departure.
No suggestion appears after reaching the station, while travelling, or during an active selected recovery.
An earlier-train suggestion uses that train's forecast and its conservative leave-by time.
If the suggested time has passed, the ordinary leave-by time remains visible without an extra-minute suggestion.
Phone, email and webhook deliveries use the same formatter at send time.
Held messages use current settings, trains and cached forecasts when sent.

**Suggest extra time for weather** in Home Assistant controls this default-on setting:

```yaml
alerts:
  weather_allowance: false
```

Turning off `weather` or `leave_by` also hides the allowance.
Turning off only `weather_allowance` keeps ordinary leave-by text and weather context.
The adjustment changes advice only. It never changes buffers, check-in times, planner queries, followed trains or deadlines.
Compensation baselines and timers also stay unchanged.
It does not select an earlier train automatically.

Status text, MCP journey text and routine messages show one adjusted leave-by statement when an allowance applies.
The canonical `leave_by` timestamp stays unchanged.
MCP journey fields and Home Assistant status attributes expose `weather_allowance` separately, with `extra_min`, `hazard` and `suggested_leave_by`.
The Home Assistant `leave_by_text` attribute contains the display text. Allowance fields are null when no allowance applies.

## Official KNMI warnings need your key

The [KNMI public weather warnings dataset, version 1.0](https://dataplatform.knmi.nl/dataset/knmi-public-weather-warnings-1-0) supplies official yellow, orange and red warnings.
Its [access instructions](https://dataplatform.knmi.nl/dataset/access/knmi-public-weather-warnings-1-0) require an Open Data API key.
No shared or anonymous key is included.

1. Request your own registered key at the [KNMI developer portal](https://developer.dataplatform.knmi.nl/open-data-api).
2. In Docker, add `KNMI_API_KEY=your-own-key` to your private `.env` file.
3. Apply Docker changes with `docker compose up -d --force-recreate tcc`.
4. In Home Assistant, use Configure > Weather and enter your key.

Clear the Home Assistant key field, or remove the Docker variable, to stop warning retrieval.
The key goes only in the Authorization header to `api.dataplatform.knmi.nl`.
It never goes to Open-Meteo or a temporary download host.
Redirects are refused. Downloads accept HTTPS hosts under `knmi.nl` or `amazonaws.com` without API credentials.
Keys and weather caches do not enter journey state or alert history.

Each published JSON file replaces the previous warning snapshot.
Schema version 0.7 supplies Polygon or MultiPolygon areas in longitude-latitude order.
Only active operational warnings covering the station and its journey time are shown.
Cancelled, expired, exercise, test and potentially-dangerous-weather records are excluded.
Severity maps to yellow, orange or red. A contradictory color or malformed snapshot makes coverage unavailable.
Official warning headlines remain in Dutch.

Without a working key or valid source, status says **KNMI warnings unavailable** and links to the public warnings page.
It never claims there are no warnings because a request failed.
An empty, valid operational snapshot can establish that no warning was published for a station and time.
Warning areas do not establish conditions between the stations or along your walk.

## Freshness and service limits

Weather refresh runs in its own cancellable background task after train decisions and delivery.
A slow or failed request does not wait inside cancellation alerts, the watchdog or storage handling.
No weather network call occurs while rendering status or sending a train message.

Each station forecast is cached in memory for less than one hour.
Failures hide the old forecast and wait at least five minutes before retrying.
Up to 32 distinct active stations are refreshed per cycle; shared stations across profiles use one forecast request.
Removed stations lose their cached forecast. A change of coordinates invalidates a cached result.

The warning dataset is checked at most once per ten minutes for the whole runtime.
Coverage is unavailable at ten minutes without another successful check.
KNMI publishes a daily heartbeat, so snapshots older than 26 hours are also refused.
Publication and retrieval timestamps are separate and remain visible.
Forecast retrieval time is not the model's issuance time.
Each request has a five-second HTTP timeout, an eight-second total deadline and a two-million-byte response limit.

The [Open-Meteo terms](https://open-meteo.com/en/terms) permit free personal home automation and other noncommercial use.
The free service limits calls to 600 per minute, 5,000 per hour and 10,000 per day.
Commercial hosting requires separate terms. This setup is a personal application, not a hosted weather service.

Weather data use [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The detailed weather report credits [Open-Meteo](https://open-meteo.com/) and the KNMI model, links the licence and identifies the companion's adapted tips.
Official warnings credit [KNMI](https://www.knmi.nl/nederland-nu/weer/waarschuwingen/).
See [Open-Meteo's attribution requirements](https://open-meteo.com/en/licence) and [KNMI's snapshot documentation](https://dataplatform.knmi.nl/dataset/docs/knmi-public-weather-warnings-1-0).
Source contracts were checked on 21 November 2026.

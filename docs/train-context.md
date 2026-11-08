# Train and platform context

Optional NS vehicle data can describe a train's listed equipment, capacity and boarding platform. It can also provide reported vehicle coordinates. These details help a traveller understand the service. They do not prove boarding, actual arrival, accessibility or available seats.

## Provider boundary

The backend provides two optional methods on `NsProvider`:

```python
await provider.train_context(train, station)
await provider.vehicle_positions()
await provider.vehicle_positions(latitude, longitude)
```

Each method performs one HTTP request. Callers must schedule it through the existing shared request budget. Global positions can serve several profiles from one retrieval. Area queries accept public station coordinates. Do not send a traveller's phone coordinates.

Train context uses `/virtual-train-api/v1/trein/{number}/{station}`. The method accepts only the current Amsterdam-local operating date. It rejects other dates, inconsistent service dates and invalid station paths before sending. It validates the response's train number and station. A response arriving after local midnight is refused when the requested operating date no longer matches.

This date restriction limits accidental cross-date reuse. The response does not corroborate the operating date. `operating_date_confirmed` remains false. Callers still need current dated journey evidence before associating context with a followed train.

Vehicle positions use `/virtual-train-api/vehicle`. With no arguments, the method requests the global feed. Both coordinates select a ten-kilometre public area with a response limit of 100 candidates. One coordinate alone is invalid. Positions are unbound candidates, not watched train identities. Duplicate service numbers fail closed.

Requests use a fixed NS host, certificate-verified HTTPS and the configured deadline. Redirects are refused. Optional responses are limited to two million decoded bytes. Product-specific 401, 403, other HTTP failures and malformed payloads raise `ContextUnavailable`. They do not establish rejection of the main journey API key. Transport failures remain sanitized `ProviderError` values. HTTP 429 retains `RateLimited` and its retry delay for shared backoff.

The runtime schedules these requests after primary journey alerts. They run in a separate owned task and share the request budget. Slow optional responses do not delay cancellation delivery. Shutdown cancels unfinished optional requests.

Only a healthy, currently corroborated locked train receives context. Proposed trains, replacement buses and resolved journeys do not. Telegram, MCP and Home Assistant status expose the shared snapshot. During recovery, this context stays labelled as the original train. It never advances recovery progress or changes a journey lock.

`VehicleMonitor` supplies optional scheduling for a caller to attach after primary journey polling. It takes a vehicle provider, the shared `RequestGate`, a clock and an optional current-watch callback. `refresh(watches)` requests one global feed and one context per distinct train and station. It reuses fresh data for two minutes and spaces failed attempts by two minutes. Requests use `RequestGate.listing`, so live journeys have priority over this optional work.

A `VehicleWatch` requires current-day train identity, a station code and a healthy dated journey check less than five minutes old. Future and stale checks are rejected. The caller must create watches only from corroborated main journey evidence. The callback is checked after provider waits. Concurrent refreshes invalidate earlier results. Removed watches lose cached context and position candidates. A newly added watch can wait until the next shared feed retrieval for its position.

`snapshot(watch, now)` hides stale context and coordinates while keeping their last successful retrieval times. A failed request never renews an old retrieval time. The snapshot reports unknown coverage and retains the measurement, equipment and capacity limits below. Only rate limits affect shared backoff; optional authentication and server errors do not become main journey failures.

## Shared presentation

`render_snapshot` describes the monitor snapshot as plain text for status surfaces. It names the requested train, station and date. It states that vehicle data does not verify that service date. Retrieval timestamps identify when the companion received data. They never stand in for measurement timestamps.

The text describes capacity as listed capacity, not free seats. A total is shown only when every stock unit has that capacity field. One missing unit value makes that total unknown. Zero remains zero. No units means unknown capacity, not zero capacity. Equipment and platform features are listed with operation explicitly unknown. Empty carriage dictionaries and pixel geometry never become carriage instructions.

A position is labelled as a candidate. Its raw speed, heading and accuracy stay in structured output because their units are unverified. Missing or hidden stale positions produce an unavailable message, without old coordinates. The last successful retrieval can still be shown to explain missing current data.

`snapshot_payload` preserves the dated journey check, separate context and position retrieval times, provider source fields and unknown coverage. It includes explicit false flags for confirmed boarding, arrival, service-date corroboration, equipment operation and seat availability. These fields do not change the monitored journey.

## What the fields mean

`TrainContext` records the request, station, retrieval time, provider source, train type, operator, platform, shortened-train status, carriage count and length. Each stock unit can have equipment labels and first-class, second-class, folding-seat and bicycle capacity counts. Missing fields remain unknown. Explicit false, zero and empty lists remain distinct from missing fields.

Seat counts describe capacity, not free seats. Equipment labels describe listed equipment, not whether it works. The platform feature list can identify stairs, escalators, lifts and platform letters. It does not establish lift operation or a step-free route. Layout direction is not an exit-side instruction. Empty carriage dictionaries do not establish carriage facilities. Remote image URLs and pixel-layout geometry are not exposed.

`VehiclePosition` preserves reported coordinates, source and raw speed, heading and accuracy values. Their units were not established from an official accessible schema, so the backend does not invent unit labels. The sampled payload carries no measurement timestamp. `measurement_at` stays null even if an unverified extra timestamp field appears.

`is_fresh` checks retrieval age against a two-minute window. It rejects future and timezone-less retrieval times. This is retrieval freshness, not measurement freshness. `payload` produces JSON-safe fields and explicit false flags for confirmed boarding, equipment operation and seat availability. Consumers must hide stale map markers and label unknown measurement time.

## Live evidence

On 6 November 2026, the configured key returned HTTP 200 for the position and station-scoped composition endpoints. A global position request returned 248 candidates in 41,143 bytes. Two public Utrecht-area requests, 49 seconds apart, returned changed coordinates for four shared service numbers. These are vehicle coordinates, distinct from the existing station coordinates.

The sampled service 579 returned a VIRM with six carriages, 161 metres, listed equipment and capacity counts. The station-scoped request for Utrecht returned track 12 and platform features. The unscoped service request instead returned Zwolle and track 6. This is why the backend always requests station-specific composition.

A `route=1776` probe returned no positions while the same service appeared in area results. The backend does not rely on that filter. No test established measurement freshness, exact operating-date binding, working equipment or carriage-by-carriage facility locations.

Public evidence was saved locally under `/private/tmp/tcc-ns-live/vehicle-*.json`. Trimmed public fixtures cover the observed response shapes in `tests/data/ns/vehicle_context.json` and `vehicle_positions.json`.

Four authenticated runtime checks also pass for Utrecht to Rotterdam and Amersfoort to Utrecht, with accessible planning enabled and disabled. Each uses a weekday departure override, explicitly selects a catchable train, refreshes optional context and saves state without credentials. These checks capture alerts locally. They do not test physical travel or phone delivery.

## Sources and limits

The [NS developer guide](https://apiportal.ns.nl/startersguide) explains product subscriptions and key-based access. Official API definitions require registration and were not available in the public static portal. Endpoint discovery used the original [NS API demo source](https://github.com/anneb/ns-api-demo/blob/main/README.md), followed by direct NS response verification. The source is discovery evidence, not official schema authority.

The [NS station facilities page](https://www.ns.nl/en/travel/at-the-station/facilities) directs travellers to its station finder and planner for facilities and opening hours. The existing station list supplies facilities and travel-assistance flags, not a detailed facilities catalogue. A general station-facilities endpoint remains unverified.

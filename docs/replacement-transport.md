# Replacement transport

NS can return replacement buses, metro rides and walking sections during engineering works. The companion reads these journeys without discarding the train information in the same answer.

Some bus and metro stops have a name and coordinates but no NS station identifier. The mapper gives those places an internal identifier beginning with `@place:`. It uses validated coordinates and the supplied name, so repeated references to the same place agree. These identifiers are not NS station codes.

Current status can show proposed bus, metro and walking instructions from fresh NS answers. Each plan keeps exact physical boarding places, explicit walks, time provenance and transfer minima. HA status attributes and MCP expose the steps; Telegram renders the instructions. These plans are labelled not followed, with an unconfirmed starting position. Accessibility and assistance remain unknown.

Telegram can prepare and follow these plans after explicit location and uncertainty acknowledgements. Preparation refreshes the station-origin query and requires the same exact physical route. A selection becomes active only after its durable save succeeds. Train-selection buttons still require a train-only journey.

Use **I boarded** and **I reached** to confirm each vehicle and walk. Time, vehicle coordinates and station zones never advance recovery progress. Duplicate actions keep the first confirmation; stale actions cannot confirm the next step. Manual confirmations remain available during NS outages.

Recovery status survives restart. Source changes require review instead of replacing the approved plan. The original train safety monitoring continues, with original-train warnings labelled separately. Routine original-train departure suggestions are suppressed while recovery is active. Today and MCP expose progress without actions.

## Home Assistant controls

Open the integration's Configure menu and choose **Recovery**. Choose a proposal and confirm your exact physical starting place. Review the refreshed itinerary, planned-time limits and unknown accessibility before following it.

The profile device then has buttons for the current **I boarded**, **I reached** and **Stop following this recovery** actions. Configure > Recovery also offers these actions with a confirmation form. Each control binds the displayed selection and step. Old controls cannot confirm a later step with the same label.

The status sensor reads `recovery_active` while recovery is active. Its attributes expose the approved itinerary, confirmation history, source check and monitoring issue. `original_train_state` describes the separate train safety watch. Recovery actions save state without editing profiles or reloading the integration.

Automations can use `prepare_recovery`, `follow_recovery`, `confirm_recovery_step` and `stop_recovery`. These services require the exact service date and selection identifiers. Responses contain the refreshed offer or saved selection. A failed save or stale action returns an error instead of confirming progress.

Planned times remain planned. A source report never proves boarding or the traveller's arrival. Check replacement transport and assistance with the NS planner before travelling. Unrestricted replanning from arbitrary locations remains unavailable with the current key.

Guidance uses only the newest answer cohort. Conflicting service evidence suppresses a plan, including cancellation, changed physical stops or changed times. This conservative rule can hide valid options. Complete service reconciliation is required before combining query cohorts. Missing, stale or future source checks also suppress guidance.

## Physical locations and planning permission

Station identifiers do not identify every boarding location. Live NS journeys show Gouda rail station and its replacement bus stop sharing `GD` and `8400258`. An explicit five-minute walk connects them. Different ADDRESS locations also share the generic UIC `8400002`. Neither equality establishes that a traveller is at the boarding stop.

The station-only provider rejects internal `@place:` identifiers and the generic ADDRESS UIC `8400002` before sending a journey query. This applies to origin, destination and via locations. Other station queries retain their existing parameters.

A live check on 6 November 2026 used public Capelsebrug coordinates, not a traveller's position. The request used `/v3/trips` with `originLat=51.921253`, `originLng=4.5579`, `originName=Metrostation Capelsebrug`, `toStation=RTD` and `dateTime=2026-11-07T13:10:00+01:00`. NS rejected it with HTTP 400 and `API_KEY_NOT_ALLOWED_TO_PLAN_DOOR_TO_DOOR`. The provider identifies this exact error as `DoorToDoorNotAllowed`, without treating the key as invalid for station journeys.

The matching station-origin control returned five journeys with replacement transport. A missing-origin control returned HTTP 400. The alternative parameter names `fromLat` and `fromLng` also returned generic HTTP 400. These checks confirm the current key's permission limit. They do not verify a working coordinate-origin query or its returned physical origin.

Coordinate-origin replanning needs permission from NS and a live returned-origin check. The companion does not expose an unused coordinate-query API. Refreshing a station-origin itinerary can provide a usable suffix only when its exact physical boarding location matches the confirmed location. It cannot establish unrestricted replanning from an ADDRESS stop.

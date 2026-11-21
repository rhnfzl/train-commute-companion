# Set up profiles with station names

Create and edit commutes in Home Assistant without looking up station codes or copying the starter file. The forms save the same portable YAML that Docker and the command line use.

## Create your first profile

1. Add Train Commute Companion under Settings, Devices and services.
2. Enter your NS API key. Leave **Use an existing YAML file** off.
3. Choose home and work stations, travel days, first trains and travel buffers. Type part of a name, such as "Utr", to search the station list. Pick a station from the list, or type its full name in any case. Text that matches no station, or more than one, is refused. Both station fields start empty, and home and work must be different stations.
4. Select transfer stations in travel order for each direction. Leave empty for a direct train.
5. Set your minimum minutes at each selected transfer station.
6. Keep existing holiday ranges, or choose Add and enter both dates.
7. Choose your phone and confirm that both test notifications arrive.

The file is created at `train_commute_companion/profiles.yaml` in the config folder, and only after you confirm both tests. An existing file is never replaced. Turn on **Use an existing YAML file** to use a file you already have.

Add more commutes later under Configure > Add or edit profiles.

## Edit or add a profile

Open the integration's Configure menu and choose **Add or edit profiles**. Pick a profile or choose **Add a profile**. Existing profile IDs remain unchanged.

A new profile gets a free name, such as "commute 2". Two profiles cannot run on the same weekday. If you pick a day that another profile uses, the form names that profile. Pick other days, or change the other profile first.

The form shows full station names from the current NS catalogue. Transfer-minimum fields identify each station in the description. Minimums preserve your saved values; a newly selected transfer starts at the core fallback of five minutes.

Holiday dates are inclusive. Choose Keep to leave ranges unchanged, Add to append a range, Replace to use only the entered range, or Remove to clear all ranges. Start and end dates are required for Add and Replace. The existing ranges are listed above the form. See [Holiday pauses](holiday-pauses.md) for overnight and already-followed journey behaviour.

The main form can request accessible journeys and disable notification categories. The details form chooses destinations for each category: the phone, configured webhooks, both, or all configured destinations. Configure webhooks before selecting them. Native email belongs to the Docker app and is unavailable in this Home Assistant form. System-health notifications remain enabled.

Existing unavailable destinations stay visible until you replace them. Saving rejects unavailable selections and keeps the file unchanged. If several profiles reference unavailable destinations, repair those mappings in YAML before saving through the forms.

Valid edits become active through the existing profile reload. Invalid edits do not change the file. If another editor changes the file while the form is open, reopen the editor before saving again.

## Settings the form preserves

The details form includes a default-on weather switch. Configure > Weather accepts your own optional KNMI warning key. See [Commute weather](weather.md).

The editor preserves settings it does not display: location, advice thresholds, quiet hours, extra dates, explicit monitoring windows, active leads (`active_lead_min`), overnight return offsets and unused transfer minimums. Other profiles stay unchanged.

## Window mode and settings by day

The main form has a mode for each direction. Fixed follows your usual train. Window follows the earliest good train between the two window times. In window mode the first train can stay blank, and the latest time is required. The same form sets the train choice and the minimum stay. See [Schedules](weekday-schedules.md#window-mode).

Turn on **Set a different mode, window, train choice or stay for some days** in the details form. A loop then edits one day at a time. Pick a day, change it, and save. Fields left on Same as every day, or blank, use the normal setting. A day with only normal settings is removed. Choose Done to finish.

A fixed profile keeps its YAML shape. The editor writes `mode`, `pick` and `min_stay_min` only when they matter.

## Other boarding stations

When the profile has a location block, the details form shows the [other boarding station](other-boarding-station.md) settings: other stations per direction, the distance, the minutes at a station before it counts, and whether to offer to remember a new station. A default is written only when it is already in the file.

## Station names in YAML

In the YAML file a station can be a code (`UT`) or a name (`Utrecht Centraal`). For a name, case, accents and punctuation do not matter, and NS short names and synonyms such as `Utrecht` also work. TCC turns each name into its code when it reads the file, so names and codes run the same. A code must be written exactly, such as `UT`.

A name that fits more than one station is refused. The error lists the matching codes, so use one of them. An unknown name gets a close suggestion. Home and work should be different stations. A code and a name for the same station count as the same station. In the file, the same station for both is only a warning, so a file from an older version still loads. The form refuses it: open that profile in the editor and pick two different stations.

TCC skips a commute with the same home and work station and writes a warning to the log. Other commutes keep running. Correct the stations in the editor or YAML file to resume monitoring.

Names need the NS station list. When it cannot be fetched, a file with names waits like any edit, and the last valid configuration keeps running. When the form saves a profile that uses names, that profile is written with codes. Other profiles keep their names.

## Weekday departure times

Turn on **Edit weekday departure times** in the details form to set different outbound and return times for each weekday. Blank fields use the direction's default time. An overnight return uses the weekday when the commute starts.

Weekday overrides shift the monitoring window by the same interval as the departure time. The core learns a separate usual route for each overridden weekday. See [Weekday schedules](weekday-schedules.md).

Explicit monitoring windows must still contain an edited default first-train time. If validation rejects that combination, adjust the window in YAML before reopening the form.

Saving uses YAML serialization. Comments, anchors and formatting can change, but validated setting values are preserved. Keep comments or manual formatting in a separate note if you need them.

New files can only be created inside the Home Assistant config folder. Existing files outside that folder can be edited only when their directory is in Home Assistant's external-directory allowlist. Files are saved atomically after validation and a check against the contents read when the form opened.

No API keys, phone tokens or webhook secrets are written into the profiles file by these forms.

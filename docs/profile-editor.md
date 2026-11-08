# Set up profiles with station names

Create and edit commutes in Home Assistant without looking up station codes or copying the starter file. The forms save the same portable YAML that Docker and the command line use.

## Create your first profile

1. Add Train Commute Companion under Settings, Devices and services.
2. Enter your NS API key and an unused profiles-file path inside the Home Assistant config folder.
3. Turn on **Create a profile with station-name forms**.
4. Choose home and work stations, travel days, first trains and travel buffers.
5. Select transfer stations in travel order for each direction. Leave empty for a direct train.
6. Set your minimum minutes at each selected transfer station.
7. Keep existing holiday ranges, or choose Add and enter both dates.
8. Choose your phone and confirm that both test notifications arrive.

The file is created only after you confirm both tests. An existing file is never replaced by guided creation. Leave the guided option off to use an existing YAML file.

## Edit or add a profile

Open the integration's Configure menu and choose **Add or edit profiles**. Pick a profile or choose **Add a profile**. Existing profile IDs remain unchanged.

The form shows full station names from the current NS catalogue. Transfer-minimum fields identify each station in the description. Minimums preserve your saved values; a newly selected transfer starts at the core fallback of five minutes.

Holiday dates are inclusive. Choose Keep to leave ranges unchanged, Add to append a range, Replace to use only the entered range, or Remove to clear all ranges. Start and end dates are required for Add and Replace. The existing ranges are listed above the form. See [Holiday pauses](holiday-pauses.md) for overnight and already-followed journey behaviour.

The main form can request accessible journeys and disable notification categories. The details form chooses destinations for each category: the phone, configured webhooks, both, or all configured destinations. Configure webhooks before selecting them. Native email belongs to the Docker app and is unavailable in this Home Assistant form. System-health notifications remain enabled.

Existing unavailable destinations stay visible until you replace them. Saving rejects unavailable selections and keeps the file unchanged. If several profiles reference unavailable destinations, repair those mappings in YAML before saving through the forms.

Valid edits become active through the existing profile reload. Invalid edits do not change the file. If another editor changes the file while the form is open, reopen the editor before saving again.

## Settings the form preserves

The editor preserves settings it does not display: location, advice thresholds, quiet hours, extra dates, explicit monitoring windows, overnight return offsets and unused transfer minimums. Other profiles stay unchanged.

Turn on **Edit weekday departure times** in the details form to set different outbound and return times for each weekday. Blank fields use the direction's default time. An overnight return uses the weekday when the commute starts.

Weekday overrides shift the monitoring window by the same interval as the departure time. The core learns a separate usual route for each overridden weekday. See [Weekday schedules](weekday-schedules.md).

Explicit monitoring windows must still contain an edited default first-train time. If validation rejects that combination, adjust the window in YAML before reopening the form.

Saving uses YAML serialization. Comments, anchors and formatting can change, but validated setting values are preserved. Keep comments or manual formatting in a separate note if you need them.

New files can only be created inside the Home Assistant config folder. Existing files outside that folder can be edited only when their directory is in Home Assistant's external-directory allowlist. Files are saved atomically after validation and a check against the contents read when the form opened.

No API keys, phone tokens or webhook secrets are written into the profiles file by these forms.

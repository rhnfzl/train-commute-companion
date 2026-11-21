"""User-facing message templates. IDs and values are independent of the language."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

MessageLanguage = Literal["en", "nl"]

_MESSAGES = {
    "status.empty": ("No journey is being followed now.", "Er wordt nu geen reis gevolgd."),
    "status.delivery_unconfirmed": (
        "Delivery not confirmed yet. Complete your phone delivery test.",
        "Meldingen zijn nog niet bevestigd. Rond de meldingentest op je telefoon af.",
    ),
    "status.key_rejected": ("Your NS key was rejected.", "NS heeft je API-sleutel geweigerd."),
    "status.profile_refused": (
        "Profile file refused: {reason}",
        "Profielbestand geweigerd: {reason}",
    ),
    "status.store_failed": (
        "State cannot be saved; retrying.",
        "Opslaan is mislukt. We proberen het opnieuw.",
    ),
    "status.pending_alerts": (
        "{count} alerts not delivered yet.",
        "Nog te bezorgen meldingen: {count}.",
    ),
    "status.health_ok": ("  NS data: fresh", "  NS-gegevens: actueel"),
    "status.health_poll_failed": (
        "  NS data: fresh, last poll failed",
        "  NS-gegevens: actueel, laatste controle mislukt",
    ),
    "status.health_stale_since": (
        "  NS data: not fresh since {time}",
        "  NS-gegevens: verouderd sinds {time}",
    ),
    "status.health_stale": ("  NS data: not fresh", "  NS-gegevens: verouderd"),
    "status.disruptions_unknown": (
        "  NS active-disruption data: unavailable; previous context may be stale.",
        "  Actuele storingen van NS zijn niet beschikbaar. Eerdere meldingen kunnen verouderd zijn.",
    ),
    "status.works_ahead": (
        "Works ahead: {name} {direction}: {dates}",
        "Werkzaamheden: {name} {direction}: {dates}",
    ),
    "status.works_not_run": (
        "Planned-works check: not run yet",
        "Werkzaamheden: nog niet gecontroleerd",
    ),
    "status.no_works": (
        "No planned works found in the commute days the last check covered.",
        "Geen geplande werkzaamheden gevonden op de laatst gecontroleerde reisdagen.",
    ),
    "status.works_stale": (
        "Planned-works check: not fresh",
        "Werkzaamheden: controle is verouderd",
    ),
    "status.works_last_ok": (
        "Planned-works check: last ok {date} {time}",
        "Werkzaamheden: laatst gecontroleerd op {date} om {time}",
    ),
    "status.source": ("Data: NS", "Gegevens: NS"),
    "direction.outbound": ("outbound", "heenreis"),
    "direction.return": ("return", "terugreis"),
    "journey.idle": ("idle", "wacht op vertrek"),
    "journey.locked": ("locked", "volgt trein"),
    "journey.riding": ("riding", "onderweg"),
    "journey.done": ("done", "afgerond"),
    "journey.expired": ("expired", "verlopen"),
    "journey.recovering": ("recovery active", "volgt vervangende reis"),
    "journey.not_travelling": (" (not travelling)", " (reist niet)"),
    "journey.route_changed": (" (route changed)", " (route gewijzigd)"),
    "arrival.target": (
        "Arrival target {target}. Reach the destination station by {deadline}.",
        "Gewenste aankomst {target}. Bereik het eindstation uiterlijk om {deadline}.",
    ),
    "arrival.late": (
        "The current train does not meet the arrival target in NS data.",
        "Volgens NS haalt de huidige trein de gewenste aankomsttijd niet.",
    ),
    "arrival.unknown": (
        "The arrival target cannot be checked from current train data.",
        "Met de huidige treingegevens is de aankomsttijd niet te controleren.",
    ),
    "arrival.no_train": (
        "No train you can catch was found for this arrival target.",
        "Er is geen haalbare trein gevonden voor deze aankomsttijd.",
    ),
    "leg.ok": ("on time per NS", "op tijd volgens NS"),
    "leg.delayed": ("delayed", "vertraagd"),
    "leg.cancelled": ("cancelled", "vervallen"),
    "leg.cut_short": ("cut_short", "rijdt niet tot de eindhalte"),
    "leg.line": ("{time} {board} to {alight}: {state}", "{time} {board} naar {alight}: {state}"),
    "leg.platform": (", platform {track}", ", spoor {track}"),
    "leg.number": (", train {number}", ", trein {number}"),
    "leg.reported_arrival": (", reported arrival {time}", ", verwachte aankomst {time}"),
    "leg.planned_arrival": (", planned arrival {time}", ", geplande aankomst {time}"),
    "leg.arrival_platform": (", arrival platform {track}", ", aankomstspoor {track}"),
    "leg.crowd": (", crowd forecast: {level} per NS", ", verwachte drukte volgens NS: {level}"),
    "leg.exit_side": (", exit side: {side} per NS", ", uitstapzijde volgens NS: {side}"),
    "leg.boarding_notes": (
        ", boarding notes per NS: {notes}",
        ", instapinformatie van NS: {notes}",
    ),
    "leg.alighting_notes": (
        ", alighting notes per NS: {notes}",
        ", uitstapinformatie van NS: {notes}",
    ),
    "leg.no_departure": (", no reported departure time", ", actuele vertrektijd onbekend"),
    "leg.no_arrival": (", no reported arrival time", ", actuele aankomsttijd onbekend"),
    "transfer.ok": ("ok", "haalbaar"),
    "transfer.at_risk": ("at_risk", "krap"),
    "transfer.broken": ("broken", "niet haalbaar"),
    "transfer.change": ("change", "overstap"),
    "transfer.original_change": ("original train change", "oorspronkelijke overstap"),
    "transfer.line": ("{label} at {station}: {state}", "{label} op {station}: {state}"),
    "transfer.margin": (" ({margin} min, needs {minimum})", " ({margin} min, minimaal {minimum})"),
    "status.retained_leg": (
        "  if you are on the {time}: ",
        "  als je in de trein van {time} zit: ",
    ),
    "status.next_connection": (
        "  next good connection: {time} from {station}{details}",
        "  volgende haalbare verbinding: {time} vanaf {station}{details}",
    ),
    "status.earlier_train": (
        "  earlier train advised: the {time}{details}",
        "  neem zo mogelijk de eerdere trein van {time}{details}",
    ),
    "leave_by": ("Leave by {time}", "Vertrek uiterlijk om {time}"),
    "leave_by.weather": (
        "{leave} (+{minutes} min suggested for forecast {hazard})",
        "{leave} (advies: {minutes} min extra bij verwachte {hazard})",
    ),
    "weather.rain": ("rain", "regen"),
    "weather.fog": ("fog", "mist"),
    "weather.snow": ("snow", "sneeuw"),
    "weather.ice": ("possible ice", "mogelijke gladheid"),
    "weather.tip_rain": ("Bring a raincoat.", "Neem een regenjas mee."),
    "weather.tip_snow": (
        "Snow expected; wear shoes with grip.",
        "Er wordt sneeuw verwacht. Draag schoenen met grip.",
    ),
    "weather.tip_heat": (
        "Heat expected; carry water and seek shade.",
        "Er wordt hitte verwacht. Neem water mee en zoek schaduw.",
    ),
    "weather.tip_frost": (
        "Frost possible; watch your footing.",
        "Er is kans op vorst. Kijk uit waar je loopt.",
    ),
    "weather.tip_ice": (
        "Ice possible; paths and platforms may be slippery.",
        "Er is kans op gladheid op paden en perrons.",
    ),
    "weather.station": (
        "Weather at {station} around {time}: ",
        "Weer bij {station} rond {time}: ",
    ),
    "weather.missing": ("forecast unavailable.", "weersverwachting niet beschikbaar."),
    "weather.forecast": (
        "{temp}°C, {rain} mm precipitation, {snow} cm snow (forecast hour {time}). ",
        "{temp} °C, {rain} mm neerslag, {snow} cm sneeuw (verwachting voor {time}). ",
    ),
    "weather.retrieved": (" Retrieved {stamp}. ", " Opgehaald op {stamp}. "),
    "weather.credit": (
        "Forecast: Open-Meteo https://open-meteo.com/ (KNMI model); tips adapted by TCC. ",
        "Verwachting van Open-Meteo https://open-meteo.com/ (KNMI-model). Tips van TCC. ",
    ),
    "weather.warning_missing": (
        " KNMI warnings unavailable; check {url}.",
        " KNMI-waarschuwingen niet beschikbaar. Kijk op {url}.",
    ),
    "weather.warning_clear": (
        "no published warning for this station and time",
        "geen waarschuwing gepubliceerd voor dit station en tijdstip",
    ),
    "weather.warning_times": (
        "Checked {checked}; published {issued}. ",
        "Gecontroleerd op {checked}. Gepubliceerd op {issued}. ",
    ),
    "shared.unknown": ("unknown", "onbekend"),
    "shared.change_at": (", change at {stations}", ", overstappen op {stations}"),
    "shared.then": (" then ", " en daarna "),
    "shared.crowd": (
        ", crowd forecasts per NS: {forecasts}",
        ", verwachte drukte volgens NS: {forecasts}",
    ),
    "checkin.all_good": (
        "your {time} to {station} is on time per NS",
        "je trein van {time} naar {station} rijdt op tijd volgens NS",
    ),
    "checked.cancellations": (
        (
            "NS shows cancellations on every train option checked from {origin} to {destination} "
            "for {day}. Check the NS app for other options."
        ),
        (
            "Volgens NS vervallen alle gecontroleerde treinopties van {origin} naar {destination} "
            "op {day}. Kijk in de NS-app voor andere opties."
        ),
    ),
    "checked.platform": ("platform {track}", "spoor {track}"),
    "checked.planned_platform": ("scheduled platform {track}", "gepland spoor {track}"),
    "checked.just_now": ("just now", "zojuist"),
    "checked.minutes_ago": ("{minutes} min ago", "{minutes} min geleden"),
    "checked.runs": ("NS checked {ago}: runs", "NS-controle {ago}: rijdt"),
    "checked.your": ("your ", "je trein van "),
    "checked.conditional": (
        "next good connection: {time}.",
        "volgende haalbare verbinding: {time}.",
    ),
    "connection.usual_left": (
        "your usual {time} has left. If you are not on it,",
        "je gebruikelijke trein van {time} is vertrokken. Zit je er niet in?",
    ),
    "connection.train_left": (
        "your {train} train has left. If you are not on it,",
        "je trein van {train} is vertrokken. Zit je er niet in?",
    ),
    "connection.no_option": (
        "no good connection found, check the NS app.",
        "Ik vond geen haalbare verbinding. Kijk in de NS-app.",
    ),
    "connection.unknown_change": (
        "check the NS app: the change from your current train is not in NS data.",
        "Kijk in de NS-app. Je aansluiting staat niet in de NS-gegevens.",
    ),
    "checked.choose": (". Choose it to follow it", ". Kies deze trein om hem te volgen"),
    "checked.current_option": (
        "Check the NS app for a current train option.",
        "Kijk in de NS-app voor een actuele treinoptie.",
    ),
    "checked.route_change": ("NS reported a route change.", "NS meldt een gewijzigde route."),
    "lookahead.journey": ("journey", "reis"),
    "lookahead.leg": ("{origin} to {destination} leg", "rit van {origin} naar {destination}"),
    "lookahead.missing": (
        "your {journey} has no usual train (works)",
        "je gebruikelijke trein voor de {journey} rijdt niet (werkzaamheden)",
    ),
    "lookahead.cancelled": (
        "your {journey} has no trains (works)",
        "er rijden geen treinen voor je {journey} (werkzaamheden)",
    ),
    "lookahead.earlier_stop": ("an earlier stop", "een eerdere halte"),
    "lookahead.cut_short": (
        "your {journey} runs only to {stop} (works)",
        "je {journey} gaat alleen tot {stop} (werkzaamheden)",
    ),
    "lookahead.via": ("via {stations}", "via {stations}"),
    "lookahead.direct": ("direct", "rechtstreeks"),
    "lookahead.pattern": ("your journey goes {route} (works)", "je reist {route} (werkzaamheden)"),
    "lookahead.short": (
        "your change at {station} is too short ({margin} min, you need {minimum})",
        "je overstap op {station} is te kort ({margin} min, je hebt {minimum} min nodig)",
    ),
    "lookahead.works": (
        "your {journey} is affected by planned works",
        "er zijn geplande werkzaamheden voor je {journey}",
    ),
    "lookahead.changed": (
        "your {journey} changes to {new}, {duration} min (was {old}, {previous} min)",
        "je {journey} vertrekt om {new} en duurt {duration} min (was {old}, {previous} min)",
    ),
    "lookahead.replacement_hint": (
        "Check replacement transport nearer the day.",
        "Controleer kort voor vertrek welk vervangend vervoer er rijdt.",
    ),
    "lookahead.other_changes": (
        "NS now offers your usual journey only with other changes; update your profile",
        "NS biedt je gebruikelijke reis alleen met andere overstappen aan. Pas je profiel aan",
    ),
    "lookahead.another_day": (
        "NS moved your {old} to {new} on another day; update your profile",
        "NS heeft je trein van {old} verplaatst naar {new} op een andere dag. Pas je profiel aan",
    ),
    "lookahead.overlap": (
        "NS moved your {old} to {new}, which overlaps another window; update your profile",
        "NS heeft je trein van {old} verplaatst naar {new}. Dat overlapt een andere reisperiode. Pas je profiel aan",
    ),
    "lookahead.withdraw_older": (
        "ignore the earlier change; I follow the newer NS time",
        "negeer de eerdere wijziging. Ik volg de nieuwere tijd van NS",
    ),
    "lookahead.withdraw": (
        "your usual timetable stays as it was; ignore the earlier change",
        "je gebruikelijke dienstregeling blijft gelden. Negeer de eerdere wijziging",
    ),
    "lookahead.adopted": (
        "NS moved your {old} to {new} from {day}; I follow the new time.",
        "NS heeft je trein van {old} verplaatst naar {new} vanaf {day}. Ik volg de nieuwe tijd.",
    ),
    "lookahead.update_profile": (" Update your profile?", " Wil je je profiel aanpassen?"),
    "lookahead.moved": (
        "NS moved your {old} to {new} from {day}",
        "NS heeft je trein van {old} verplaatst naar {new} vanaf {day}",
    ),
    "lookahead.moved_status": (
        "{name}.{direction}: {text}; update your profile?",
        "{name} {direction}: {text}. Wil je je profiel aanpassen?",
    ),
    "lookahead.shift": (
        "your usual {old} departure is listed at {new}. Check NS for your journey",
        "je gebruikelijke trein van {old} staat gepland om {new}. Controleer je reis bij NS",
    ),
    "lookahead.restored": (
        "your {journey} is back to normal",
        "je {journey} rijdt weer volgens de gebruikelijke dienstregeling",
    ),
    "lookahead.refused": (
        "{name}: profile change refused: {reason}.",
        "{name}: profielwijziging geweigerd: {reason}.",
    ),
    "lookahead.yaml": (
        'From {day}, set {field} to "{time}" in the profiles.yaml entry with id "{profile}".',
        'Stel vanaf {day} in profiles.yaml bij profiel "{profile}" het veld {field} in op "{time}".',
    ),
    "lookahead.yaml_optional": (
        "If {field} is set, change it to {hours}.",
        "Als {field} is ingesteld, wijzig het dan in {hours}.",
    ),
    "lookahead.yaml_required": ("Set {field} to {hours}.", "Stel {field} in op {hours}."),
    "unclear.train": ("your train", "je trein"),
    "unclear.station": (" at {station}", " op {station}"),
    "unclear.heading": (
        "NS data unclear for {train}{station}.",
        "Onduidelijke NS-gegevens voor {train}{station}.",
    ),
    "unclear.copy": (" Copy {number}: {details}.", " Melding {number}: {details}."),
    "unclear.check": (
        " Check the NS app or the station boards.",
        " Kijk in de NS-app of op de vertrekborden.",
    ),
    "recovery.proposal": (
        "Proposed recovery itinerary (not followed)",
        "Voorgestelde vervangende reis (nog niet gevolgd)",
    ),
    "recovery.planned_only": (
        " (unconfirmed: planned times)",
        " (geplande tijden, nog niet bevestigd)",
    ),
    "recovery.accessibility": (
        "Accessibility and assistance availability are not verified.",
        "Toegankelijkheid en beschikbare reisassistentie zijn niet gecontroleerd.",
    ),
    "recovery.change": (
        "Change vehicle: {walk} min walking, {wait} min waiting.",
        "Overstappen: {walk} min lopen, {wait} min wachten.",
    ),
    "recovery.step": (
        "{times} {service}: {origin} to {destination}.",
        "{times} {service}: van {origin} naar {destination}.",
    ),
    "recovery.origin_prompt": (
        "Confirm your exact physical starting place. No recovery is selected yet.",
        "Bevestig je exacte vertrekpunt. Je hebt nog geen vervangende reis gekozen.",
    ),
    "recovery.physical_id": ("physical ID {id}", "locatie-ID {id}"),
    "recovery.at": ("I am at {place}", "Ik ben bij {place}"),
    "recovery.no_origin": (
        "No unambiguous physical starting place is available. Check the NS planner.",
        "Er is geen eenduidig vertrekpunt beschikbaar. Kijk in de NS-reisplanner.",
    ),
    "recovery.refreshed": ("Refreshed recovery offer.", "Bijgewerkt reisvoorstel."),
    "recovery.refreshed_at": (
        "Refreshed recovery offer checked {time}.",
        "Bijgewerkt reisvoorstel, gecontroleerd om {time}.",
    ),
    "recovery.follow_accepts": (
        "Follow accepts any planned-only times shown above and unverified accessibility and assistance.",
        "Als je deze reis volgt, accepteer je de geplande tijden en dat toegankelijkheid en reisassistentie niet zijn gecontroleerd.",
    ),
    "recovery.progress_limits": (
        "Only your explicit boarding and arrival confirmations change progress.",
        "Alleen jouw bevestigingen van instappen en aankomst werken je voortgang bij.",
    ),
    "recovery.follow": ("Follow this recovery (accept limits above)", "Volg deze vervangende reis"),
    "recovery.progress": (
        "Recovery {status}. Progress uses only your explicit confirmations.",
        "Vervangende reis: {status}. Jij bevestigt de voortgang.",
    ),
    "recovery.active": ("active", "actief"),
    "recovery.completed": ("completed", "afgerond"),
    "recovery.stopped": ("stopped", "gestopt"),
    "recovery.last_place": (
        "Last confirmed place: {place} at {time}.",
        "Laatst bevestigde locatie: {place} om {time}.",
    ),
    "recovery.current_step": (
        "Current step:{times} {mode} {operator} {service}: {origin} to {destination}.",
        "Huidig traject:{times} {mode} {operator} {service}: van {origin} naar {destination}.",
    ),
    "recovery.next": ("Next confirmation: {action}.", "Volgende bevestiging: {action}."),
    "recovery.boarded": ("boarded", "ingestapt"),
    "recovery.arrived": ("arrived", "aangekomen"),
    "recovery.walk_arrived": ("walk arrived", "te voet aangekomen"),
    "recovery.refresh_failed": (
        "Recovery data refresh failed. Previous times may be stale; manual confirmations remain available.",
        "De vervangende reis kon niet worden bijgewerkt. Eerdere tijden kunnen verouderd zijn. Je kunt je voortgang nog bevestigen.",
    ),
    "recovery.review": (
        "NS changed the recovery route. Review the planner before continuing.",
        "NS heeft de vervangende route gewijzigd. Kijk in de reisplanner voordat je verder reist.",
    ),
    "recovery.i_boarded": (
        "I boarded {operator} {mode} {service}",
        "Ik ben ingestapt: {operator} {mode} {service}",
    ),
    "recovery.i_arrived": ("I reached {place}", "Ik ben aangekomen bij {place}"),
    "recovery.stop": ("Stop recovery", "Stop deze vervangende reis"),
    "recovery.review_origin": (
        "Review recovery starting place",
        "Bekijk het vertrekpunt van de vervangende reis",
    ),
    "recovery.checked": (" checked {time}", ", gecontroleerd om {time}"),
    "recovery.plan": (
        "  NS replacement plan{checked}; starting position not confirmed:",
        "  Vervangende reis volgens NS{checked}. Vertrekpunt nog niet bevestigd:",
    ),
    "button.leaving": ("I'm leaving", "Ik vertrek"),
    "button.skip": ("Not travelling", "Ik reis niet"),
    "button.take": ("Take {time}", "Neem de trein van {time}"),
    "command.profile_missing": (
        "No profile {name}. One of: {names}.",
        "Profiel {name} bestaat niet. Kies uit: {names}.",
    ),
    "command.profile_which": (
        "Which profile? One of: {names}.",
        "Welk profiel bedoel je? Kies uit: {names}.",
    ),
    "command.help": (
        (
            "/status: the journeys being followed now\n"
            "/stats [week|month|year]: your recorded commute statistics\n"
            "/clearstats: review clearing your local commute statistics\n"
            "/trip UT RTD: check the next trains between two stations\n"
            "/plan: set up a one-off or repeating trip\n"
            "/plans: view, edit or cancel saved trips\n"
            "/agents: choose which agents can change plans\n"
            "/preferences: view saved travel preferences\n"
            "/remember: save a travel preference\n"
            "/forgetpref NAME: delete a travel preference\n"
            "/choose [profile] [outbound|return] HH:MM [train number] [YYYY-MM-DD]: lock that train\n"
            "/leaving [profile] [outbound|return]: I'm leaving now\n"
            "/arrived: enter and review an actual destination arrival with a full date and timezone\n"
            "/skip [profile] [outbound|return|day] [YYYY-MM-DD]: not travelling, alerts for it stop\n"
            "/stations: the boarding stations TCC remembers\n"
            "/forget STATION: stop remembering that boarding station\n"
            "/setup: create your first commute when using the Docker installer\n"
            "/test: send the loud and the silent test alert (and test each webhook)\n"
            "/help: this list\n"
            "Data: NS"
        ),
        (
            "/status: bekijk de reizen die nu worden gevolgd\n"
            "/stats [week|month|year]: bekijk je woon-werkstatistieken\n"
            "/clearstats: controleer en wis je lokale statistieken\n"
            "/trip UT RTD: bekijk de eerstvolgende treinen tussen twee stations\n"
            "/plan: plan een eenmalige of terugkerende reis\n"
            "/plans: bekijk, wijzig of annuleer opgeslagen reizen\n"
            "/agents: kies welke assistenten reisplannen mogen wijzigen\n"
            "/preferences: bekijk opgeslagen reisvoorkeuren\n"
            "/remember: sla een reisvoorkeur op\n"
            "/forgetpref NAAM: verwijder een reisvoorkeur\n"
            "/choose [profiel] [outbound|return] UU:MM [treinnummer] [JJJJ-MM-DD]: volg die trein\n"
            "/leaving [profiel] [outbound|return]: ik vertrek nu\n"
            "/arrived: controleer en bewaar je aankomsttijd met datum en tijdzone\n"
            "/skip [profiel] [outbound|return|day] [JJJJ-MM-DD]: stop meldingen voor die reis\n"
            "/stations: bekijk je opgeslagen vertrekstations\n"
            "/forget STATION: verwijder een opgeslagen vertrekstation\n"
            "/setup: maak je eerste woon-werkprofiel met het Docker-installatieprogramma\n"
            "/test: verstuur een testmelding met en zonder geluid en controleer elke webhook\n"
            "/help: toon deze lijst\n"
            "Gegevens: NS"
        ),
    ),
    "command.unknown_input": ("I did not understand {input}.", "Ik begrijp {input} niet."),
    "command.choose_usage": (
        "Usage: /choose [profile] [outbound|return] HH:MM [train number] [YYYY-MM-DD]",
        "Gebruik: /choose [profiel] [outbound|return] UU:MM [treinnummer] [JJJJ-MM-DD]",
    ),
    "command.trip_usage": (
        "Use /trip UT RTD, /trip Utrecht Centraal -> Rotterdam Centraal, or /trip saved NAME.",
        "Gebruik /trip UT RTD, /trip Utrecht Centraal -> Rotterdam Centraal of /trip saved NAAM.",
    ),
    "trip.saved_missing": (
        "No saved travel preference by that name. See /preferences.",
        "Er is geen reisvoorkeur opgeslagen met die naam. Kijk bij /preferences.",
    ),
    "trip.station_value": (
        "Enter an NS station code or name of up to 80 characters.",
        "Voer een stationscode of stationsnaam van maximaal 80 tekens in.",
    ),
    "trip.station_ambiguous": (
        "{value} matches several stations: {choices}. Use a station code.",
        "{value} komt overeen met meerdere stations: {choices}. Gebruik een stationscode.",
    ),
    "trip.station_unknown": (
        "Unknown NS station: {value}. Use its full name or station code.",
        "Onbekend NS-station: {value}. Gebruik de volledige naam of stationscode.",
    ),
    "trip.station_same": (
        "Choose different origin and destination stations.",
        "Kies verschillende vertrek- en eindstations.",
    ),
    "trip.stale": (
        "The NS trip data is stale or has an invalid check time. Try again.",
        "De reisinformatie van NS is verouderd of het controletijdstip is ongeldig. Probeer het opnieuw.",
    ),
    "trip.cooldown": (
        "Wait 30 seconds between trip checks.",
        "Wacht 30 seconden tussen reiscontroles.",
    ),
    "trip.disabled": (
        "Quick trip checks are disabled for this companion by a profile.",
        "Snelle reiscontroles zijn uitgeschakeld in een profiel.",
    ),
    "trip.settings_pending": (
        "The companion is still checking its settings. Try again.",
        "De companion controleert de instellingen nog. Probeer het opnieuw.",
    ),
    "trip.auth": (
        "NS rejected your API key. Check the NS-App key.",
        "NS heeft je API-sleutel geweigerd. Controleer de sleutel voor NS-App.",
    ),
    "trip.catalogue": (
        "The NS station list is unavailable. Try again.",
        "De stationslijst van NS is niet beschikbaar. Probeer het opnieuw.",
    ),
    "trip.unavailable": (
        "NS trip data is unavailable. Try again.",
        "Reisinformatie van NS is niet beschikbaar. Probeer het opnieuw.",
    ),
    "trip.scheduled": ("scheduled", "gepland"),
    "trip.reported": ("reported", "gemeld"),
    "trip.unknown": ("unavailable", "onbekend"),
    "trip.leg": (
        "{train} {origin} to {destination}. Departs {departure}. Arrives {arrival}.",
        "{train} van {origin} naar {destination}. Vertrek {departure}. Aankomst {arrival}.",
    ),
    "trip.route": ("{origin} to {destination}", "{origin} naar {destination}"),
    "trip.checked": (
        "NS check {date} at {time} (Amsterdam time).",
        "NS-controle op {date} om {time} (Nederlandse tijd).",
    ),
    "trip.platform_reported": (" Platform {platform} reported.", " Gemeld spoor: {platform}."),
    "trip.platform_scheduled": (" Platform {platform} scheduled.", " Gepland spoor: {platform}."),
    "trip.crowd": (" Crowd forecast: {level}.", " Verwachte drukte: {level}."),
    "trip.no_options": (
        "NS returned no usable upcoming train options for this route.",
        "NS gaf geen bruikbare treinopties voor deze route.",
    ),
    "command.timed_out": (
        "NS did not answer in time; try again.",
        "NS antwoordde niet op tijd. Probeer het opnieuw.",
    ),
    "command.failed": (
        "That failed; see the app log.",
        "Dat is niet gelukt. Kijk in het logboek van de app.",
    ),
    "command.invalid_button": ("That button is no longer valid.", "Deze knop is niet meer geldig."),
    "command.locked": ("Locked the {time} for {name}.", "Ik volg de trein van {time} voor {name}."),
    "command.answer_saved": ("Answer saved.", "Antwoord opgeslagen."),
    "command.leaving": ("Marked as leaving now.", "Je vertrek is vastgelegd."),
    "command.skip_day": (
        "Not travelling today: alerts for both directions stop.",
        "Je reist vandaag niet. Meldingen voor beide richtingen stoppen.",
    ),
    "command.skip_trip": (
        "Not travelling: alerts for that journey stop.",
        "Je maakt deze reis niet. De meldingen voor deze reis stoppen.",
    ),
    "command.forget_usage": (
        "Usage: /forget STATION, for example /forget VL",
        "Gebruik: /forget STATION, bijvoorbeeld /forget VL",
    ),
    "command.forgot": ("Forgot {station}.", "{station} is niet langer opgeslagen."),
    "command.forgot_some": (
        "Forgot {station} for some trips, but: {error}",
        "{station} is voor een deel van de reizen verwijderd. {error}",
    ),
    "command.station_unsaved": (
        "{station} is not a remembered boarding station.",
        "{station} is geen opgeslagen vertrekstation.",
    ),
    "command.test_both": ("I got both", "Ik kreeg beide meldingen"),
    "command.test_loud": (
        "Train Commute Companion: test alert, loud. You should hear this one.",
        "Train Commute Companion: testmelding met geluid. Je moet deze melding horen.",
    ),
    "command.test_silent": (
        "Train Commute Companion: test alert, silent. This one should arrive without sound.",
        "Train Commute Companion: stille testmelding. Je moet deze melding zonder geluid ontvangen.",
    ),
    "wake.now": (
        "Wake now for the {time} train. {reason}",
        "Sta nu op voor de trein van {time}. {reason}",
    ),
    "wake.timetable": (
        "Your usual departure moved earlier.",
        "Je gebruikelijke trein vertrekt eerder.",
    ),
    "wake.cancelled": ("Your usual train is cancelled.", "Je gebruikelijke trein vervalt."),
    "wake.delayed": ("Your usual train is delayed.", "Je gebruikelijke trein heeft vertraging."),
    "partner.eta": (
        "Train {minutes} min late. Expected arrival around {time}.",
        "Trein {minutes} min vertraagd. Verwachte aankomst rond {time}.",
    ),
    "navigation.open": (
        "Open your boarding station ({station}) in Maps.",
        "Open je vertrekstation ({station}) in Kaarten.",
    ),
    "arrival.question": (
        "{name}: NS reported your train at {station} at {time}. Did you arrive about then?",
        "{name}: Volgens NS kwam je trein om {time} aan op {station}. Kwam je rond die tijd aan?",
    ),
    "arrival.yes": ("Yes", "Ja"),
    "station.no": ("No", "Nee"),
    "station.other_train": ("Other train", "Andere trein"),
    "station.remember": (
        "remember {station} as a boarding station for your {direction} trips?",
        "{station} onthouden als vertrekstation voor je {direction}?",
    ),
    "station.pick": ("which train from {station}?", "welke trein neem je vanaf {station}?"),
    "station.ask": (
        "are you taking the {time} from {origin} to {destination}?",
        "neem je om {time} de trein van {origin} naar {destination}?",
    ),
    "train.missing": (
        "your {train} train is not in NS data.",
        "je trein van {train} staat niet in de NS-gegevens.",
    ),
    "train.usual_missing": (
        "your usual {time} is not in NS data.",
        "je gebruikelijke trein van {time} staat niet in de NS-gegevens.",
    ),
    "train.selected_missing": (
        "NS did not show your selected {time}{number}",
        "NS toont je gekozen trein van {time}{number} niet",
    ),
    "train.selected_ambiguous": (
        "More than one train matches your selected {time}{number}",
        "Meerdere treinen komen overeen met je gekozen trein van {time}{number}",
    ),
    "train.number": (" (train {number})", " (trein {number})"),
    "train.check_app": (" Check the NS app.", " Kijk in de NS-app."),
    "relock.option_short": (" Next option: {time}.", " Volgende optie: {time}."),
    "train.no_arrival": (
        "no train you can catch was found for arrival by {time}, check the NS app.",
        "Ik vond geen haalbare trein voor aankomst om {time}. Kijk in de NS-app.",
    ),
    "train.window_empty": (
        "no train found from {start} to {end}, check the NS app.",
        "Ik vond geen trein tussen {start} en {end}. Kijk in de NS-app.",
    ),
    "arrival.reopened_cancelled": (
        "{on}your {train} train is cancelled; following it again.",
        "{on}je trein van {train} vervalt. Ik volg de trein opnieuw.",
    ),
    "arrival.reopened_short": (
        "{on}your {train} train no longer reaches {stop}; following it again.",
        "{on}je trein van {train} rijdt niet meer tot {stop}. Ik volg de trein opnieuw.",
    ),
    "arrival.reopened_later": (
        "{on}NS now shows your {train} train arriving at {time}; following it again.",
        "{on}NS meldt nu aankomst om {time} voor je trein van {train}. Ik volg de trein opnieuw.",
    ),
    "recovery.original_monitoring": ("Original train monitoring: ", "Oorspronkelijke trein: "),
    "recovery.original_state": (
        "  Original train monitoring state: {state}.",
        "  Status van de oorspronkelijke trein: {state}.",
    ),
    "recovery.original_legs": (
        "  Original train legs (separate from selected recovery):",
        "  Trajecten van de oorspronkelijke trein:",
    ),
    "recovery.original_retained": (
        "  Original retained train warnings:",
        "  Meldingen over eerder gevolgde treinen:",
    ),
    "recovery.original_vehicle": (
        "  Original train vehicle context (not the selected recovery):",
        "  Treingegevens voor de oorspronkelijke reis:",
    ),
    "choice.refused": ("choice refused: {reason}.", "Treinkeuze geweigerd: {reason}."),
    "choice.no_answer": (
        "no fresh NS answer to check it against",
        "geen actuele gegevens van NS om de trein te controleren",
    ),
    "choice.arrival": (
        "this train no longer meets your arrival plan",
        "deze trein haalt je gewenste aankomsttijd niet meer",
    ),
    "choice.not_train": ("not train only", "de reis bestaat niet alleen uit treinen"),
    "choice.boarding": (
        "does not board at the boarding station",
        "de trein vertrekt niet vanaf je vertrekstation",
    ),
    "choice.destination": (
        "does not arrive at the destination",
        "de trein rijdt niet tot je eindstation",
    ),
    "choice.transfer_cap": ("over the transfer cap", "te veel overstappen"),
    "choice.cancelled": ("leg cancelled", "een trein vervalt"),
    "choice.cut_short": ("leg cut_short", "een trein stopt eerder"),
    "choice.uncertain": ("leg uncertain", "een trein is niet bevestigd"),
    "choice.missing": ("leg missing", "een trein ontbreekt in de NS-gegevens"),
    "choice.missing_time": (
        "leg has a planned or missing time",
        "NS geeft alleen geplande tijden of geen tijden voor een trein",
    ),
    "choice.too_late": ("first leg cannot be caught", "je kunt de eerste trein niet meer halen"),
    "choice.change_unknown": (
        "the change from your current train is not in NS data",
        "NS vermeldt de aansluiting vanaf je huidige trein niet",
    ),
    "choice.transfer": (
        "transfer at {station} {state}: {reason}",
        "overstap op {station} is {state}: {reason}",
    ),
    "arrival.other": ("Other time", "Andere tijd"),
    "arrival.saved": ("Arrival saved. ", "Aankomst opgeslagen. "),
    "arrival.after": ("{minutes} minutes after", "{minutes} minuten later dan"),
    "arrival.before": ("{minutes} minutes before", "{minutes} minuten eerder dan"),
    "arrival.compensation": (
        (
            "Your confirmed arrival at {station} is {comparison} your saved plan. "
            "Check current NS compensation rules. Adjusted timetables and other conditions may affect your claim. "
            "This comparison does not decide eligibility. {url}"
        ),
        (
            "Je bevestigde aankomst op {station} is {comparison} je opgeslagen planning. "
            "Controleer de actuele voorwaarden voor vergoeding bij NS. Een aangepaste dienstregeling en andere voorwaarden kunnen invloed hebben op je aanvraag. "
            "NS bepaalt of je recht hebt op vergoeding. {url}"
        ),
    ),
    "arrival.details": ("Journey details", "Reisgegevens"),
    "arrival.date": ("Travel date", "Reisdatum"),
    "arrival.origin": ("Planned origin", "Gepland vertrekstation"),
    "arrival.destination": ("Destination", "Eindstation"),
    "arrival.trains": ("Planned trains", "Geplande treinen"),
    "arrival.planned_departure": ("Planned departure", "Gepland vertrek"),
    "arrival.planned_arrival": ("Planned arrival", "Geplande aankomst"),
    "arrival.reported": ("NS-reported train arrival", "Aankomst van de trein volgens NS"),
    "arrival.confirmed": ("Your confirmed arrival", "Je bevestigde aankomst"),
    "stats.empty": (
        "There are no recorded journeys in this period.",
        "Er zijn geen reizen opgeslagen voor deze periode.",
    ),
    "stats.arrivals": (
        "{on_time} of {arrivals} recorded NS arrivals were on time ({percent}%). Average delay {delay} minutes.",
        "{on_time} van {arrivals} door NS gemelde aankomsten waren op tijd ({percent}%). Gemiddelde vertraging: {delay} minuten.",
    ),
    "stats.unavailable": (
        "There is no comparable NS arrival time in this period.",
        "Er is geen vergelijkbare aankomsttijd van NS voor deze periode.",
    ),
    "stats.worst": (" Highest average delay on {day}.", " Hoogste gemiddelde vertraging op {day}."),
    "stats.journeys": (
        "{count} recorded journeys. {arrivals}{worst} Cancelled or cut short {cancelled}, moved {moved}, missed {missed}.",
        "{count} opgeslagen reizen. {arrivals}{worst} Vervallen of ingekort: {cancelled}, verplaatst: {moved}, gemist: {missed}.",
    ),
    "stats.heading": ("Commute statistics", "Woon-werkstatistieken"),
    "stats.period": ("Last {days} days", "Afgelopen {days} dagen"),
    "stats.this_week": ("This week. ", "Deze week. "),
    "stats.recorded_arrival": ("NS recorded arrival at {time}.", "NS meldt aankomst om {time}."),
    "stats.on_time": (
        " No delay against the saved plan.",
        " Geen vertraging volgens de opgeslagen planning.",
    ),
    "stats.delay": (
        " {minutes} minutes after the saved arrival time.",
        " {minutes} minuten na de opgeslagen aankomsttijd.",
    ),
    "today.title": ("Today", "Vandaag"),
    "today.intro": (
        "Your current journeys and service health. This page is read-only.",
        "Je huidige reizen en de status van de dienst. Deze pagina is alleen-lezen.",
    ),
    "today.snapshot": ("Snapshot", "Momentopname"),
    "today.refresh": ("Refresh status", "Status vernieuwen"),
    "today.resume": ("Resume auto-refresh", "Automatisch vernieuwen hervatten"),
    "today.pause": ("Pause auto-refresh", "Automatisch vernieuwen pauzeren"),
    "today.interval": ("Updates every minute.", "Ververst elke minuut."),
    "today.journeys": ("Journeys and service health", "Reizen en status van de dienst"),
    "today.ready": ("Ready", "Gereed"),
    "today.starting": ("Starting", "Bezig met opstarten"),
    "today.confirmed": ("Confirmed", "Bevestigd"),
    "today.unconfirmed": ("Not confirmed", "Niet bevestigd"),
    "today.state": (
        "Companion: {service}. Phone delivery: {delivery}.",
        "Companion: {service}. Telefoonmeldingen: {delivery}.",
    ),
    "today.freshness": ("Data freshness", "Actualiteit van de gegevens"),
    "today.fresh": ("fresh", "actueel"),
    "today.stale": ("not fresh", "verouderd"),
    "today.unknown": ("unknown", "onbekend"),
    "today.checked": (
        "{name}, {direction}: NS data {freshness}; last successful NS check: {checked}.",
        "{name}, {direction}: NS-gegevens {freshness}. Laatste geslaagde NS-controle: {checked}.",
    ),
    "today.no_checks": (
        "No current journey has a provider freshness report.",
        "Voor de huidige reizen is geen tijdstip van de laatste gegevenscontrole beschikbaar.",
    ),
    "today.alerts": ("Recent alerts", "Recente meldingen"),
    "today.no_alerts": ("No recent alerts saved.", "Geen recente meldingen opgeslagen."),
    "today.save_journey": ("Save one journey", "Een reis opslaan"),
    "today.calendar_download": (
        "Download this journey to calendar",
        "Download deze reis voor je agenda",
    ),
    "today.preview_download": (
        "Download a route preview to share",
        "Download een routevoorbeeld om te delen",
    ),
    "today.tentative": (
        "Download one tentative saved plan. It is not live and does not update.",
        "Download een voorlopig opgeslagen reisplan. Het bestand wordt niet bijgewerkt.",
    ),
    "today.preview_privacy": (
        "The route preview includes stations, trains and planned dates. Share the downloaded file deliberately; it opens without a login.",
        "Het routevoorbeeld bevat stations, treinen en geplande reisdagen. Deel het bestand alleen met wie het mag zien. Inloggen is niet nodig.",
    ),
    "today.recovery": (
        "Unselected replacement plans are proposals, not followed journeys. Followed recovery progress uses your explicit confirmations. A timetable does not confirm boarding or arrival.",
        "Je volgt een vervangende reis pas nadat je die kiest. Daarna bevestig je zelf de voortgang. Een dienstregeling bevestigt geen instap of aankomst.",
    ),
    "today.telegram": (
        "Use your private Telegram chat for journey actions. Data: NS.",
        "Gebruik je privéchat in Telegram om een reis aan te passen. Gegevens: NS.",
    ),
    "today.facilities": (
        "Use {link} for toilets, shops and opening hours. Listed equipment does not establish accessibility or working lifts.",
        "Kijk bij {link} voor toiletten, winkels en openingstijden. Controleer toegankelijkheid en de werking van liften apart.",
    ),
    "today.facilities_label": ("NS station facilities", "stationsvoorzieningen van NS"),
    "brief.heading": (
        "{name}: commute brief for {date}.",
        "{name}: reisoverzicht voor {date}.",
    ),
    "brief.train": ("{time} train {number}", "{time} trein {number}"),
    "brief.cut_short": ("cut short", "rijdt niet tot de eindhalte"),
    "brief.cancelled": ("cancelled", "vervallen"),
    "brief.connection": (
        "Your connection at {station} is broken. Check NS for your journey",
        "Je aansluiting op {station} is niet haalbaar. Controleer je reis bij NS",
    ),
    "brief.arrive_by": (
        "No confirmed train meets your {time} arrival target",
        "Geen bevestigde trein haalt je gewenste aankomsttijd van {time}",
    ),
    "brief.listed": (
        "{train} listed. Check NS for your journey",
        "{train} staat in de dienstregeling. Controleer je reis bij NS",
    ),
    "brief.scheduled": (
        "{time} scheduled. Check NS for your journey",
        "Gepland om {time}. Controleer je reis bij NS",
    ),
    "brief.window": (
        "Travel window {start} to {end}. Train not yet confirmed",
        "Reisvenster van {start} tot {end}. Trein nog niet bevestigd",
    ),
    "brief.details_unavailable": (
        "{time} scheduled. NS train details unavailable",
        "Gepland om {time}. Treingegevens van NS niet beschikbaar",
    ),
    "brief.works": (
        "NS planned works on this route: {titles}",
        "Geplande werkzaamheden van NS op dit traject: {titles}",
    ),
    "brief.works_changed": (
        "NS reported a timetable change on this route. Check NS for your journey",
        "NS meldt een wijziging op dit traject. Controleer je reis bij NS",
    ),
    "brief.works_unknown": ("Planned works data unavailable", "Werkzaamheden niet beschikbaar"),
    "brief.disruption": ("NS disruption: {title}", "Storing bij NS: {title}"),
    "brief.forecast": (
        (
            "{station} forecast {temp}°C, {rain} mm precipitation"
            " (Open-Meteo https://open-meteo.com/, CC BY 4.0)"
        ),
        (
            "Verwachting bij {station}: {temp} °C, {rain} mm neerslag"
            " (Open-Meteo https://open-meteo.com/, CC BY 4.0)"
        ),
    ),
    "digest.title": (
        "{name}: weekly planned-works digest {first} to {last}.",
        "{name}: weekoverzicht werkzaamheden van {first} tot {last}.",
    ),
    "digest.started": ("NS checks started {time}.", "NS-controles gestart op {time}."),
    "digest.dates": (
        "Dates refer to commute dates; overnight service dates are shown separately.",
        "Het overzicht gebruikt je reisdagen. Nachtritten tonen hun eigen datum.",
    ),
    "digest.service_date": (" (service date {date})", " (ritdatum {date})"),
    "digest.check_again": (
        "These checks are not a service guarantee. Check NS again before travelling.",
        "Controleer NS opnieuw voor vertrek. De dienstregeling kan nog wijzigen.",
    ),
    "digest.works": ("NS planned works: {title}", "Geplande werkzaamheden van NS: {title}"),
    "digest.route_unknown": ("route coverage uncertain", "routegegevens onzeker"),
    "digest.affected": (
        "planned works may affect the checked route",
        "geplande werkzaamheden kunnen dit traject beïnvloeden",
    ),
    "digest.clear": (
        "no planned-works indication found in these checks",
        "bij deze controles zijn geen werkzaamheden gevonden",
    ),
    "digest.effects": (
        "Timetable effects reported: {effects}. Cause not verified.",
        "Gemelde wijzigingen in de dienstregeling: {effects}. Oorzaak onbekend.",
    ),
    "digest.coverage_unknown": (
        "works or route coverage uncertain",
        "werkzaamheden of routegegevens onzeker",
    ),
    "digest.incomplete": ("unknown: check did not finish", "onbekend: controle onvolledig"),
    "digest.not_scheduled": ("not scheduled", "geen reis gepland"),
    "digest.paused": ("paused", "gepauzeerd"),
    "digest.request_failed": (
        "unknown: NS request failed or was deferred",
        "onbekend: NS-aanvraag mislukt of uitgesteld",
    ),
    "digest.invalid": (
        "unknown: source evidence does not match this check",
        "onbekend: NS-gegevens komen niet overeen met de controle",
    ),
    "digest.reason.missing": ("missing", "trein ontbreekt"),
    "digest.reason.pattern": ("pattern", "ander traject"),
    "digest.reason.cancelled": ("cancelled", "trein vervallen"),
    "digest.reason.cut_short": ("cut_short", "rit ingekort"),
    "digest.reason.messages": ("messages", "reisadvies van NS"),
    "digest.reason.maintenance": ("maintenance", "werkzaamheden"),
    "digest.reason.short": ("short", "onvoldoende overstaptijd"),
    "health.uncertain": (
        "the train is uncertain in the answer",
        "NS geeft geen uitsluitsel over je trein",
    ),
    "health.missing": (
        "the train is missing in the answer",
        "je trein ontbreekt in de NS-gegevens",
    ),
    "health.no_departure": ("no reported departure time", "actuele vertrektijd onbekend"),
    "health.no_arrival": ("no reported arrival time", "actuele aankomsttijd onbekend"),
    "health.no_fresh_train": (
        "the train is not in a fresh answer",
        "je trein ontbreekt in de actuele NS-gegevens",
    ),
    "health.old_board": (
        "the board is older than 5 minutes",
        "de treingegevens zijn ouder dan 5 minuten",
    ),
    "health.failed_polls": ("the last polls failed", "de laatste controles zijn mislukt"),
    "health.blind": (
        "cannot see your train in NS data ({reason}), check the NS app.",
        "je trein is niet zichtbaar in de NS-gegevens ({reason}). Kijk in de NS-app.",
    ),
    "health.recovered": (
        "your train is visible in NS data again.",
        "je trein is weer zichtbaar in de NS-gegevens.",
    ),
    "health.offline": (
        "NS data is unavailable, check the NS app.",
        "NS-gegevens zijn niet beschikbaar. Kijk in de NS-app.",
    ),
    "journey.retained_prefix": (
        "if you are on the {time}: ",
        "als je in de trein van {time} zit: ",
    ),
    "journey.stopped": (
        "I stopped following your journey, check the NS app.",
        "Ik volg je reis niet meer. Kijk in de NS-app.",
    ),
    "journey.retained_stopped": (
        "I stopped following it, check the NS app.",
        "Ik volg deze trein niet meer. Kijk in de NS-app.",
    ),
    "journey.arrived_unconfirmed": (
        (
            "NS showed your train arriving at {time}; I stopped following, check the NS "
            "app if you are not there."
        ),
        "Volgens NS kwam je trein om {time} aan. Ik volg de reis niet meer. Kijk zo nodig in de NS-app.",
    ),
    "journey.retained_arrived": (
        "NS showed it arriving at {time}; I stopped following it, check the NS app.",
        "Volgens NS kwam deze trein om {time} aan. Ik volg de trein niet meer. Kijk in de NS-app.",
    ),
    "train.subject": ("your {train}", "je trein van {train}"),
    "train.route": ("{time} {board} to {destination}", "{time} {board} naar {destination}"),
    "train.delay": (
        "{subject} train is delayed {minutes} min",
        "{subject} heeft {minutes} min vertraging",
    ),
    "train.delay_split": (
        "{subject} train leaves {minutes} min late and arrives {arrival}",
        "{subject} vertrekt {minutes} min te laat en komt {arrival} aan",
    ),
    "train.on_time": ("on time", "op tijd"),
    "train.minutes_late": ("{minutes} min late", "{minutes} min te laat"),
    "train.arriving": (", arriving {time}", ", aankomst om {time}"),
    "train.back_to_normal": (
        "{subject} train is back to normal, on time per NS.",
        "{subject} rijdt weer op tijd volgens NS.",
    ),
    "train.nearly_on_time": (
        "{subject} train is nearly on time.",
        "{subject} rijdt bijna op tijd.",
    ),
    "train.platform_restored": (
        "{subject} train leaves from platform {track} again, as planned.",
        "{subject} vertrekt weer van het geplande spoor {track}.",
    ),
    "train.platform_changed": (
        "{subject} train now leaves from platform {track} (was {old}).",
        "{subject} vertrekt nu van spoor {track} (was spoor {old}).",
    ),
    "train.crowd_alert": (
        "{subject} train has a {level} crowd forecast per NS.",
        "{subject} heeft volgens NS een {level} verwachte drukte.",
    ),
    "crowd.low": ("low", "lage"),
    "crowd.medium": ("medium", "gemiddelde"),
    "crowd.high": ("high", "hoge"),
    "disruption.until": ("until about {time}{date}", "tot ongeveer {time}{date}"),
    "disruption.date": (" on {date}", " op {date}"),
    "disruption.active": (
        "NS reports {title} involving your route's stations{until}; check your journey.",
        "NS meldt {title} bij stations op je route{until}. Controleer je reis.",
    ),
    "disruption.summary": (
        "NS: {summary}{until} Check your journey.",
        "NS: {summary}{until} Controleer je reis.",
    ),
    "disruption.cleared": (
        (
            "NS no longer lists {title} as active. Check your journey; "
            "this does not confirm that your trains run."
        ),
        (
            "NS meldt {title} niet langer als actieve storing. Controleer je reis. "
            "Of je treinen rijden, is nog niet bevestigd."
        ),
    ),
    "status.disruption": (
        "  NS disruption context: {title} Possible route impact; check your journey.",
        "  Storing bij NS: {title} Mogelijk gevolg voor je route. Controleer je reis.",
    ),
    "advice.leave_now": (
        (
            "leave now, an earlier train {origin} to {destination} leaves at {time}"
            " and gets you there sooner than your usual journey"
        ),
        (
            "vertrek nu. Een eerdere trein van {origin} naar {destination} vertrekt om {time}."
            " Daarmee ben je er eerder dan met je gebruikelijke reis"
        ),
    ),
    "advice.withdrawn": ("earlier train no longer needed", "de eerdere trein is niet meer nodig"),
    "train.destination": ("{time} to {station}", "{time} naar {station}"),
    "train.arrives": (", arrives {time}", ", aankomst om {time}"),
    "train.at_station": (
        "at the station: your {train}.",
        "Op het station volg ik je trein van {train}.",
    ),
    "train.at_named_station": (
        "at {station}: following your {train}.",
        "Bij {station} volg ik je trein van {train}.",
    ),
    "departure.now": ("Leave now for the {time} train.", "Vertrek nu voor de trein van {time}."),
    "departure.extra": (
        " Allow {minutes} extra minutes for forecast {hazard}.",
        " Reken op {minutes} minuten extra bij verwachte {hazard}.",
    ),
    "alert.end": ("{cause}.", "{cause}."),
    "train.cancelled": ("{subject} train is cancelled", "{subject} vervalt"),
    "train.short_turn": (
        "{subject} train runs only to {end}, change there",
        "{subject} rijdt tot {end}. Stap daar over",
    ),
    "train.end_restored": (
        "back to normal: {subject} train reaches {end} again",
        "{subject} rijdt weer tot {end}",
    ),
    "train.restored": (
        "back to normal: {subject} train runs again.",
        "{subject} rijdt weer.",
    ),
    "train.route_changed": (
        "NS now routes {subject} train via {via}",
        "NS leidt {subject} nu om via {via}",
    ),
    "train.direct": ("a direct train", "een rechtstreekse trein"),
    "train.following": ("{cause}, following it.", "{cause}. Ik volg deze reis."),
    "train.follow_via": (" via {via}, following it.", " om via {via}. Ik volg deze reis."),
    "train.via_prefix": (" via ", " om via "),
    "train.missed": ("you missed the {time}", "je hebt de trein van {time} gemist"),
    "train.missed_change": (
        "you missed your change at {station} to the {train} train",
        "je hebt op {station} je aansluiting op de trein van {train} gemist",
    ),
    "transfer.warning": (
        "your change at {station} is broken: {reason}",
        "je aansluiting op {station} is niet haalbaar: {reason}",
    ),
    "transfer.risk": (
        "your change at {station} is at risk: {reason}.",
        "je aansluiting op {station} is onzeker: {reason}.",
    ),
    "transfer.reason.outgoing leg cancelled": (
        "outgoing leg cancelled",
        "aansluitende trein vervalt",
    ),
    "transfer.reason.incoming leg cancelled": (
        "incoming leg cancelled",
        "aankomende trein vervalt",
    ),
    "transfer.reason.incoming leg cut short before the transfer station": (
        "incoming leg cut short before the transfer station",
        "aankomende trein stopt vóór het overstapstation",
    ),
    "transfer.reason.incoming leg does not call at the transfer station": (
        "incoming leg does not call at the transfer station",
        "aankomende trein stopt hier niet",
    ),
    "transfer.reason.outgoing leg does not board at the transfer station": (
        "outgoing leg does not board at the transfer station",
        "aansluitende trein vertrekt hier niet",
    ),
    "transfer.reason.provider: change not possible": (
        "provider: change not possible",
        "volgens NS niet mogelijk",
    ),
    "transfer.reason.provider: not reachable": (
        "provider: not reachable",
        "volgens NS niet haalbaar",
    ),
    "transfer.reason.margin below minimum": ("margin below minimum", "te weinig overstaptijd"),
    "transfer.reason.leg not reliably seen": ("leg not reliably seen", "trein niet bevestigd"),
    "transfer.reason.a train is not reliably identified": (
        "a train is not reliably identified",
        "een trein is niet met zekerheid geïdentificeerd",
    ),
    "transfer.reason.planned or missing time": (
        "planned or missing time",
        "alleen een geplande tijd of geen tijd",
    ),
    "transfer.reason.margin below minimum plus 2": (
        "margin below minimum plus 2",
        "minder dan 2 minuten speling",
    ),
    "relock.cap": (
        "{cause}: no good connection within your usual changes; following NS via {via}{extra}",
        "{cause}. Geen haalbare verbinding met je gebruikelijke aantal overstappen. Ik volg NS via {via}{extra}",
    ),
    "relock.extra_legs": (
        ", the extra legs are beyond the sensors.",
        ". De extra ritten verschijnen niet in de sensoren.",
    ),
    "relock.unknown_change": (
        "{cause}; the change from your current train is not in NS data, check the NS app.",
        "{cause}. NS vermeldt de aansluiting vanaf je huidige trein niet. Kijk in de NS-app.",
    ),
    "relock.option": (
        " Next option: {time}{arrival}. Choose it to follow it.",
        " Volgende optie: {time}{arrival}. Kies deze trein om hem te volgen.",
    ),
    "relock.option_label": ("Next option: {time}{ending}.", "Volgende optie: {time}{ending}."),
    "alert.append": ("{cause}.{clause}", "{cause}.{clause}"),
    "alert.join": ("{cause} {clause}", "{cause} {clause}"),
    "relock.no_replacement": (
        "{cause}. No replacement found, check the NS app.",
        "{cause}. Geen vervangende trein gevonden. Kijk in de NS-app.",
    ),
    "relock.no_arrival": (
        "{cause}. No {which} train you can catch was found for arrival by {time}.",
        "{cause}. Ik vond geen haalbare {which} trein voor aankomst om {time}.",
    ),
    "relock.onward": ("onward", "aansluitende"),
    "relock.earlier": ("earlier", "eerdere"),
    "relock.no_catchable": (
        "{cause}. No train you can still catch was found, check the NS app.",
        "{cause}. Geen trein gevonden die je nog kunt halen. Kijk in de NS-app.",
    ),
    "relock.no_connection": (
        "no connection found, check the NS app.",
        "geen verbinding gevonden. Kijk in de NS-app.",
    ),
    "relock.new_route": (", new route via {via}", ", nieuwe route via {via}"),
    "relock.take": (
        " Take the {which} train at {time} from {station}",
        " Neem de {which} trein van {time} vanaf {station}",
    ),
    "relock.take_prefix": (" Take the ", " Neem de "),
    "relock.next": (" Next: {train}.", " Volgende trein: {train}."),
    "relock.can_catch": (
        " Next you can catch: {time}{route}",
        " Volgende haalbare trein: {time}{route}",
    ),
    "relock.next_good": (
        " Next good connection: {time}{route}",
        " Volgende haalbare verbinding: {time}{route}",
    ),
    "relock.platform": (" (platform {track})", " (spoor {track})"),
    "relock.then": (
        ", then the {time} from {station}{platform}",
        ", daarna de trein van {time} vanaf {station}{platform}",
    ),
    "relock.following": (
        "Now following your {time} train{onward}.",
        "Ik volg nu je trein van {time}{onward}.",
    ),
    "arrival.no_longer_meets": (
        "your train no longer meets your {time} arrival target",
        "je trein haalt je gewenste aankomsttijd van {time} niet meer",
    ),
    "arrival.not_confirmed": (
        "NS has no confirmed arrival time for your {time} target",
        "NS heeft geen bevestigde aankomsttijd voor je gewenste aankomst om {time}",
    ),
}
_NL_WEEKDAYS = ("ma", "di", "wo", "do", "vr", "za", "zo")
_EN_FULL_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_NL_FULL_WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
_NL_MONTHS = ("jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec")


def message(key: str, language: MessageLanguage, **values: str | int) -> str:
    return _MESSAGES[key][1 if language == "nl" else 0].format(**values)


def common_language(languages: Iterable[MessageLanguage]) -> MessageLanguage:
    found = set(languages)
    return next(iter(found)) if len(found) == 1 else "en"


@dataclass(frozen=True, slots=True)
class Phrase:
    key: str
    values: dict[str, str | int | Phrase]

    def render(self, language: MessageLanguage) -> str:
        return message(
            self.key,
            language,
            **{
                name: value.render(language) if isinstance(value, Phrase) else value
                for name, value in self.values.items()
            },
        )


def phrase(key: str, **values: str | int | Phrase) -> Phrase:
    return Phrase(key, values)


def message_weekday(weekday: int, language: MessageLanguage) -> str:
    return (_NL_FULL_WEEKDAYS if language == "nl" else _EN_FULL_WEEKDAYS)[weekday]


def message_date(day: date, language: MessageLanguage, *, full_weekday: bool = False) -> str:
    if full_weekday:
        month = _NL_MONTHS[day.month - 1] if language == "nl" else f"{day:%b}"
        return f"{message_weekday(day.weekday(), language)} {day.day} {month}"
    if language == "nl":
        return f"{_NL_WEEKDAYS[day.weekday()]} {day.day} {_NL_MONTHS[day.month - 1]}"
    return f"{day:%a} {day.day} {day:%b}"

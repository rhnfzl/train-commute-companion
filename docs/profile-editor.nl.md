# Profielen instellen in Home Assistant

Kies bij de installatie je stations, reisdagen en tijden. Open later **Configureren > Profielen toevoegen of wijzigen** om deze gegevens aan te passen.

## Locatie

Kies in het formulier met extra instellingen een persoon om Home Assistant als locatiebron te gebruiken. Laat de persoon leeg om de huidige locatiebron te behouden.

Kies daarna een thuis- en werkzone. Laat de thuiszone leeg om `zone.home` te gebruiken. De werkzone is optioneel.

Kies eventueel een Wi-Fi-tracker per zone en een bewegingssensor. Home Assistant kan een sensor voorstellen als de persoon aan één mobiel apparaat gekoppeld is. Kies een Wi-Fi-tracker die meldt of je telefoon met het netwerk van die zone verbonden is. Maak een veld leeg om die keuze te wissen.

Kies **Locatie via Home Assistant uitschakelen** om met tijden te blijven werken. De opgeslagen locatiegegevens blijven beschikbaar. Selecteer de persoon opnieuw om locatie in te schakelen.

## Stille uren en weekoverzicht

Vul voor stille uren twee verschillende tijden in, bijvoorbeeld `22:00` en `07:00`. Gebruik Nederlandse tijd. Maak beide velden leeg om stille uren uit te schakelen. Meldingen met geluid blijven binnenkomen.

Kies een dag en tijd voor het weekoverzicht van geplande werkzaamheden. Kies **Uit** voor de dag om dit overzicht uit te schakelen.

## Stations en de volgende treinreis

Home Assistant toont maximaal drie stations in de buurt van `zone.home` bovenaan. Je kunt nog steeds in de hele stationslijst zoeken. Je woonlocatie en telefoonlocatie worden niet naar NS gestuurd.

Als NS een passende reis levert, zie je vóór het opslaan de datum, vertrektijd, het bekende vertrekspoor en eventuele overstapstations. De app gebruikt je reisdagen, aangepaste tijden, pauzes en feestdagen. Bij een flexibel tijdvenster zie je een mogelijke trein. Als de dienstregeling ontbreekt of de aanvraag te lang duurt, kun je verder zonder dit voorbeeld.

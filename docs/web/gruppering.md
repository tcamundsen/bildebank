# Gruppering

Gruppering lager forslag til bilder som ligner på hverandre. Forslagene
endrer ikke bildefiler, tagger, personer, steder, kommentarer eller
importopplysninger.

## Start en kjøring

1. Åpne Bildebank-vinduet med `bildebank start`.
2. Velg bildesamlingen og åpne fanen **Verktøy**.
3. Trykk **Grupper bilder …**.
4. Velg antall grupper. Du kan også skrive et filtersøk og endre seed.
5. Trykk **Start gruppering**.

Et tomt filter bruker alle aktive bilder. Valget **Skjul "Ute av fokus"** er
av som standard. Jobben bruker embeddings som allerede er laget av
**Klargjør bildesøk**. Bilder uten en gyldig embedding hoppes over og telles i
resultatet.

## Se eller slett resultatet

Åpne **Gruppering** i bildebrowseren. Der ser du kjøringer, status og
gruppene i hver kjøring. **Vis alle bildene** åpner den vanlige
bildebrowseren for gruppen.

En kjøring kan slettes fra kjøringssiden etter bekreftelse. Bare selve
gruppeforslaget slettes. Bilder, metadata og bildesøkdata beholdes.

Gruppering er tilgjengelig lokalt, også når den lokale serveren er
skrivebeskyttet. Sidene vises ikke ved LAN-deling, og en skrivebeskyttet
server kan ikke slette kjøringer.

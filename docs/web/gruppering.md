# Gruppering

Gruppering lager forslag til bilder som ligner på hverandre. Forslagene
endrer ikke bildefiler, tagger, personer, steder, kommentarer eller
importopplysninger.

## Start en kjøring

1. Åpne Bildebank-vinduet med `bildebank start`.
2. Velg bildesamlingen og åpne fanen **Verktøy**.
3. Trykk **Grupper bilder …**.
4. Velg algoritme og innstillinger. Du kan også skrive et filtersøk.
5. Trykk **Start gruppering**.

**MiniBatchKMeans** er standardvalget. Her velger du antall grupper og kan
endre seed. Algoritmen er rask og egner seg godt når du vet omtrent hvor mange
grupper du ønsker.

**HDBSCAN** velger antall grupper automatisk. Du velger minste gruppestørrelse
og kan la **Min samples** stå tomt for å bruke samme verdi. Bilder som ikke
passer tydelig i en gruppe, vises separat som **Ugrupperte bilder**. HDBSCAN kan
bruke vesentlig mer tid og minne enn MiniBatchKMeans på store samlinger.

Et tomt filter bruker alle aktive bilder. Valget **Skjul "Ute av fokus"** er
av som standard. Jobben bruker embeddings som allerede er laget av
**Klargjør bildesøk**. Bilder uten en gyldig embedding hoppes over og telles i
resultatet.

## Se eller slett resultatet

Åpne **Gruppering** i bildebrowseren. Der ser du kjøringer, status og
gruppene i hver kjøring. **Vis alle bildene** åpner den vanlige
bildebrowseren for gruppen.

En kjøring kan slettes fra oversikten eller kjøringssiden etter bekreftelse.
Bare selve gruppeforslaget slettes. Bilder, metadata og bildesøkdata beholdes.

Gruppering er tilgjengelig lokalt, også når den lokale serveren er
skrivebeskyttet. Sidene vises ikke ved LAN-deling, og en skrivebeskyttet
server kan ikke slette kjøringer.

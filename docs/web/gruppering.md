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
passer tydelig i en gruppe, vises separat som **Ugrupperte bilder**.  Et godt
forlag er minste gruppestørrelse på 8 og **Min samples** på 3.  HDBSCAN kan
bruke vesentlig mer tid og minne enn MiniBatchKMeans på store samlinger.

**Leiden** bygger en graf av bilder som ligner hverandre og finner grupper i
grafen. Du velger antall naboer og CPM-oppløsning. Høyere oppløsning gir
vanligvis flere og mindre grupper. De øvrige tekniske innstillingene bruker
testede standardverdier. Bilder som ikke får noen forbindelse til andre bilder,
vises som **Ugrupperte bilder**.

Et tomt filter bruker alle aktive bilder. Valget **Skjul "Ute av fokus"** er
av som standard. Jobben bruker embeddings som allerede er laget av
**Klargjør bildesøk**. Bilder uten en gyldig embedding hoppes over og telles i
resultatet.

Mens algoritmen arbeider, oppdateres én linje i launcherloggen hvert sekund med
forløpt tid. Oppdateringen drives av launcheren og fortsetter selv når
selve beregningen ikke kan rapportere fremdrift. HDBSCAN kan ikke gi en
pålitelig prosent eller beregnet gjenstående tid. Leiden rapporterer hvilket
trinn som arbeider, men ikke beregnet gjenstående tid.

## Se eller slett resultatet

Åpne **Gruppering** i bildebrowseren. Panelet for hver kjøring viser status,
algoritme, de viktigste innstillingene og hvilket utvalg som ble brukt. Åpne
en kjøring for å se alle lagrede parametere og gruppene i kjøringen. **Vis alle
bildene** åpner den vanlige bildebrowseren for gruppen.

En kjøring kan slettes fra oversikten eller kjøringssiden etter bekreftelse.
Bare selve gruppeforslaget slettes. Bilder, metadata og bildesøkdata beholdes.

Gruppering er tilgjengelig lokalt, også når den lokale serveren er
skrivebeskyttet. Sidene vises ikke ved LAN-deling, og en skrivebeskyttet
server kan ikke slette kjøringer.

# run-server

Dette er dokumentasjon for en enkel lokal Bildebank-server.

Målet er å kunne bruke Bildebank i nettleseren på samme PC, og særlig å kunne
søke med OpenCLIP uten å vente på at modellen lastes på nytt for hvert søk.

## Mål

- Kjøre en lokal server fra en bildesamling.
- Browse hele bildesamlingen i nettleseren.
- Holde OpenCLIP-modellen lastet i minnet mens serveren kjører.
- Søke etter bilder fra nettleseren.
- Vise søkeresultater uten å skrive ny statisk HTML-fil for hvert søk.

## Start serveren

```powershell
bildebank run-server
```

Serveren bør som standard bare lytte lokalt:

```text
127.0.0.1
```

Det gjør at den kan brukes fra samme PC, men ikke deles på nettverket ved et
uhell.

Standardporten er:

```text
8765
```

Når serveren kjører, åpne:

```text
http://127.0.0.1:8765/
```

Terminalen skriver først at serveren starter, og deretter:

```text
Bildebank-serveren er klar: http://127.0.0.1:8765/
```

Nettleseren åpnes først når serveren er klar til å svare.

Hvis du vil velge port:

```powershell
bildebank run-server --port 8766
```

Hvis du ikke vil åpne nettleseren automatisk:

```powershell
bildebank run-server --no-browser
```

## Første versjon

Første versjon er enkel:

- start serveren fra bildesamlingen
- åpne første bilde/video i samlingen
- gi hvert bilde/video en stabil URL basert på `file_id`
- la månedsoversikten laste bare filene i den måneden
- vis lenke til OpenCLIP-søk fra browseren
- last OpenCLIP-modellen første gang den trengs
- behold modellen i minnet etter første søk
- kjør søk uten å starte programmet på nytt
- returner resultater som HTML

## Hvorfor server

I dag kjører `bildebank image-search` som en egen kommando. Da må Python starte,
config leses, og OpenCLIP-modellen lastes før søket kan kjøres.

Med en server kan denne kostnaden betales én gang:

1. Start `bildebank run-server`.
2. Serveren laster modellen ved første søk.
3. Senere søk bruker samme modell i minnet.

Dette bør gjøre flere søk etter hverandre merkbart raskere.

Første søk kan fortsatt ta noen sekunder, fordi OpenCLIP-modellen lastes første
gang den trengs. Senere søk i samme serverprosess bruker modellen som allerede
ligger i minnet.

Serverbrowseren skal ikke generere én stor HTML-side med hele samlingen. Den
viser ett bilde/video om gangen, og månedsoversikten henter bare filene for den
aktuelle måneden.

## Mulige sider

- `/` bildebrowser
- `/item/123` viser ett bestemt bilde eller video
- `/month/2024-01` viser bilder/videoer fra én måned
- `/people` viser registrerte personer
- `/person/Kari/confirmed` viser bilder der Kari er bekreftet
- `/person/Kari` viser både bekreftede bilder og bilder foreslått av
  `face-suggest`
- `/search` skjema for tekstsøk
- `/search?q=beach` søkeresultat
- `/file/...` viser bildefiler fra søket
- `/file/123` viser selve bildefilen for ett `file_id`

Når du åpner `Ansikter i bildet` i bildebrowseren, kan du enten velge en
registrert person eller skrive inn et nytt navn under `Ny person` og trykke
`Identifiser`. Da oppretter serveren personen og kobler ansiktet til personen.

Knappen `Bildeinfo` i bildebrowseren viser filnavn, filstørrelse, oppløsning,
kamera hvis dette finnes i metadata, og hvilke kilder som inkluderer bildet.
Overlayet kan lukkes med `Lukk` eller Esc.

## Viktige valg

- Serveren skal være lokal som standard.
- Den skal ikke kreve internett etter at modeller er lastet ned.
- Den skal bruke samme config som resten av Bildebank.
- Den skal ikke erstatte statiske HTML-filer i første omgang.
- Den statiske browseren fra `make-browser` skal beholdes.
- Den skal ikke åpne for redigering eller sletting i første versjon.

## Ikke i første versjon

- innlogging
- deling på nettverk
- redigering av metadata
- import fra nettleser
- sletting eller flytting av bilder
- live `image-scan` fra nettleser
- flere samtidige brukere

## Teknisk retning

Serveren bruker en liten lokal HTTP-server fra Python-standardbiblioteket. Hvis
behovet vokser kan vi vurdere FastAPI eller lignende senere.

OpenCLIP-modellen ligger i serverprosessen, slik at den kan brukes om igjen
mellom søk. Serveren tåler at modellen ikke er lastet ennå, og gir en lesbar
feil hvis OpenCLIP ikke er installert eller `image-scan` ikke er kjørt.

Bildebrowseren bruker samme underliggende database som `make-browser`, men har
egen serverflyt med stabile `file_id`-URL-er. Det gjør at et bilde kan bokmerkes
og åpnes igjen senere.

## Stoppe serveren

Stopp serveren i terminalen med:

```text
Ctrl-C
```

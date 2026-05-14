# run-server

Dette er dokumentasjon for den lokale Bildebank-serveren.

Dette er vanligvis den beste måten å se på bildene på egen PC.
[`make-browser`](make-browser.md) og [`open-browser`](open-browser.md) kan brukes
hvis man vil se på bildene uten å ha Bildebank installert, for eksempel fra en
ekstern disk på en annen PC.

`run-server` gir også flere funksjoner som ikke er mulig med den statiske
HTML-filen laget av `make-browser`.

## Funksjoner

- Bla gjennom hele bildesamlingen i nettleseren.
- Slette bilder. (Foreløpig bare flytte til `deleted/`)
- Rotere bilder som har feil orientering. Info om rotasjon lagres i
  databasen og bildet roteres i nettleser. Dvs at originale bildefiler
  ikke endres.
- Holde OpenCLIP-modellen lastet i minnet mens serveren kjører, slik at
  man slipper ventetid ved oppstart, og søk kan gjøres i nettleseren.
  Dette gjør at søkene går raskere etter første søk.
- Registrere personer og knytte gode bilder til dem.

## Start serveren

```powershell
bildebank run-server
```

Når serveren har startet, åpnes nettsiden automatisk i nettleseren når serveren
er klar til å svare.

Standard adresse er `http://127.0.0.1:8765/`.

Denne adressen gjør at den kan brukes fra samme PC, men ikke deles på
nettverket ved et uhell.

Hvis du vil dele bildesamlingen på LAN, må du åpne brannmuren og starte serveren slik:

```powershell
bildebank run-server --host 0.0.0.0
```

Og så må du finne IP-adressen til laptopen som kjører serveren med `ipconfig`.
Hvis adressen er 192.168.86.11, så skriver du `http://192.168.86.11:8765/` i
adressefeltet til nettleseren.

Hvis du vil velge port:

```powershell
bildebank run-server --port 8766
```
Hvis du ikke vil åpne nettleseren automatisk:

```powershell
bildebank run-server --no-browser
```

## Litt om bruk

Når du åpner `Ansikter i bildet` i bildebrowseren, kan du enten velge en
registrert person eller skrive inn et nytt navn under `Ny person` og trykke
`Identifiser`. Da oppretter serveren personen og kobler ansiktet til personen.
Se også den samlede innføringen: [`insightface`](insightface.md).


Knappen `Bildeinfo` i bildebrowseren viser filnavn, filstørrelse, oppløsning,
kamera hvis dette finnes i metadata, og hvilke kilder som inkluderer bildet.
Overlayet kan lukkes med `Lukk` eller Esc.

På bildesider kan du bruke `Roter venstre` og `Roter høyre` for å rotere
visningen av bildet. Bildebank lagrer bare rotasjonen i databasen. Selve
bildefilen i samlingen endres ikke.

Knappen `Slett` flytter bildet til `deleted`-mappen i bildesamlingen og
markerer filen som slettet i databasen. Dette er samme trygge sletting som
kommandoen `bildebank remove`. Bildefilen slettes ikke permanent.

## Teknisk info

Serveren bruker en liten lokal HTTP-server fra Python-standardbiblioteket. Hvis
behovet vokser, kan vi vurdere FastAPI eller lignende senere. Serveren har ikke
innebygd sikkerhet, og bør bare kjøres lokalt på laptop eller på et privat LAN
der man har kontroll på brukerne.

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

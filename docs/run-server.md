# run-server

<!-- CLI-HELP-START -->
```text
usage: bildebank run-server [valg]

options:
  -h, --help    show this help message and exit
  --host HOST   Adresse serveren lytter på. Standard: 127.0.0.1
  --port PORT   Port serveren lytter på. Standard: 8765
  --no-browser  Ikke åpne serveren automatisk i nettleser.
```
<!-- CLI-HELP-END -->

`run-server` starter den lokale bildebrowseren for Bildebank. Dette er
en lokal web-server som kjører på din PC, og som du ser i nettleseren.
Dette er vanligvis den beste måten å se på bildene på egen PC.

Den statiske HTML-browseren laget med [`make-browser`](make-browser.md) kan
fortsatt brukes når bildesamlingen skal åpnes på en maskin uten installert
Bildebank.  `run-server` gir også flere funksjoner som ikke er mulig med den
statiske HTML-filen laget av `make-browser`.

## Funksjoner

- Bla gjennom hele bildesamlingen i nettleseren.
- Slette bilder. (Foreløpig bare flytte til `deleted/`)
- Rotere bilder som har feil orientering. Informasjon om rotasjon lagres i
  databasen, og bildet roteres i nettleseren. Originale bildefiler endres ikke.
- Vise bilder gruppert etter sted når GPS-data er scannet med
  `bildebank geo-scan`.
- Vise importerte kilder og åpne en bildebrowser som bare viser bildene som
  kom fra én bestemt kilde.
- Holde OpenCLIP-modellen lastet i minnet mens serveren kjører, slik at
  modellen ikke må lastes inn på nytt ved hvert søk.
  Dette gjør at søkene går raskere etter første søk.
- Registrere personer og knytte gode bilder til dem.

## Start serveren

```powershell
bildebank run-server
Starter Bildebank-server. Dette kan ta noen sekunder.
Bildesamling: C:\Users\TA487\code\bilde-samling
Bildebank-serveren er klar: http://127.0.0.1:8765/
Trykk Ctrl-C for å stoppe serveren.
Åpner nettleser.

```

Når serveren har startet, åpnes nettsiden automatisk i nettleseren når serveren
er klar til å svare. I eksempelet over er det tatt med output fra `run-server`.
Der ser du adressen du skal åpne i nettleseren, hvis den ikke åpner seg av seg
selv: [http://127.0.0.1:8765](http://127.0.0.1:8765/).

Denne adressen gjør at den kan brukes fra samme PC, men ikke deles på
nettverket ved et uhell.

Hvis du vil dele bildesamlingen på LAN, må du åpne brannmuren og starte serveren slik:

```powershell
bildebank run-server --host 0.0.0.0
```

Og så må du finne IP-adressen til PC-en som kjører serveren med `ipconfig`.
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

Knappen `Bildeinfo` i bildebrowseren viser filnavn, dato og om datoen kommer
fra metadata, filnavn eller mtime, filstørrelse, oppløsning, kamera hvis dette
finnes i metadata, hvilke kilder som inkluderer bildet, og lenker til
stedssidene for H3-nivåene bildet er del av når bildet har GPS-data.
Overlayet kan lukkes med `Lukk` eller Esc.

På bildesider kan du bruke `Roter venstre` og `Roter høyre` for å rotere
visningen av bildet. Bildebank lagrer bare rotasjonen i databasen. Selve
bildefilen i samlingen endres ikke.

Lenken `Steder` viser lokale geo-sider basert på GPS-data som allerede er
lagret i databasen. Kjør først:

```powershell
bildebank geo-scan
```

Etterpå kan `http://127.0.0.1:8765/geo` vise områder med bilder,
`http://127.0.0.1:8765/geo/missing` vise bilder uten GPS, og `Bildeinfo`
på bildesidene kan lenke til stedssidene bildet er del av. Serveren endrer
ikke bildefilene og gjør ikke oppslag mot eksterne karttjenester.

Lenken `Kilder` viser importene Bildebank kjenner til. Fra kildesiden kan du
åpne en bildebrowser som bare viser aktive bilder fra én bestemt kilde. Dette er
nyttig når du vil kontrollere bilder fra for eksempel én mobil, USB-brikke eller
mappe.

Knappen `Slett` flytter bildet til `deleted`-mappen i bildesamlingen og
markerer filen som slettet i databasen. Dette er samme slettemekanisme
som kommandoen [`bildebank remove`](remove.md).

## Teknisk info

Serveren bruker en liten lokal HTTP-server fra Python-standardbiblioteket. Hvis
behovet vokser, kan vi vurdere FastAPI eller lignende senere. Serveren har ikke
innebygd sikkerhet, og bør bare kjøres lokalt på PC-en eller på et privat LAN
der man har kontroll på brukerne.

OpenCLIP-modellen ligger i serverprosessen, slik at den kan brukes om igjen
mellom søk. Serveren håndterer at modellen ikke er lastet ennå, og gir en lesbar
feil hvis OpenCLIP ikke er installert eller `image-scan` ikke er kjørt.

Bildebrowseren bruker samme underliggende database som `make-browser`, men har
egen serverflyt med stabile `file_id`-URL-er. Det gjør at et bilde kan bokmerkes
og åpnes igjen senere.

## Stoppe serveren

Stopp serveren i terminalen med:

```text
Ctrl-C
```

# run-server

<!-- CLI-HELP-START -->
```text
usage: bildebank run-server [valg]

Start Bildebank-server som lar deg se bildene i nettleser.

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
Bildebank. Men `run-server` gir flere funksjoner som ikke er mulig med den
statiske HTML-filen laget av `make-browser`.


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

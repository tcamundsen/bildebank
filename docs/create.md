# create
<!-- CLI-HELP-START -->
```text
usage: bildebank create [valg] mappe

Opprette en bildesamling i mappen du velger

positional arguments:
  mappe       Mappen som skal bli bildesamling

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`create` oppretter en ny bildesamling i mappen du velger.  Bildebank oppretter
databasen i bildesamlingsmappen.
Databasen lagrer informasjon om hvilke kilder som er lagt til, hvilke filer som
er importert og hvor filene ligger i bildesamlingen

Lag en tom mappe og opprett bildesamlingen:

```powershell
mkdir "C:\Users\deg\BildeSamling"
bildebank create "C:\Users\deg\BildeSamling"
```

Hvis du allerede står i mappen som skal bli bildesamling, kan du bruke:

```powershell
bildebank create .
```

Punktum betyr "mappen jeg står i nå".

## Viktig

Bildesamlingen må ligge i en egen mappe, ikke inni programmappen til
Bildebank.

Ikke bruk en mappe som allerede inneholder masse andre filer du vil rydde
manuelt i. Bildebank kommer til å lage årsmappene, månedsmappene, databasen og
HTML-filer i denne mappen.

## Vanlig arbeidsflyt

```powershell
cd "C:\Users\deg\BildeSamling"
bildebank create .
```

Når dette er gjort, kan du kontrollere at Bildebank finner bildesamlingsmappen:

```powershell
bildebank status
```

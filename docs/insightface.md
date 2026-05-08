# InsightFace

NB: Denne funksjonen er ikke ferdig ennå, så det er foreløpig bortkastet
for andre enn Tom Cato å teste dette.

InsightFace er en valgfri testkomponent for ansiktsgjenkjenning.

Vanlig Bildebank-installasjon installerer ikke InsightFace, og
ansiktsgjenkjenning er av som standard.

## Installere testkomponenten

Kjør dette fra programmappen:

```powershell
.\install-insightface.ps1
```

Scriptet installerer InsightFace og ONNX Runtime i Bildebanks lokale
Python-miljø i `.venv`. Det installerer ikke noe som et vanlig Windows-program.

Scriptet lager også `bildebank-config.toml` hvis filen mangler.

## Config

Utgangspunktet ligger i:

```text
bildebank-config.example.toml
```

Den lokale filen heter:

```text
bildebank-config.toml
```

Den lokale filen skal ikke legges i Git.

For testing kan den se slik ut:

```toml
[face_recognition]
enabled = false
provider = "cpu"
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
```

`enabled = false` betyr at Bildebank ikke bruker ansiktsgjenkjenning.

## Status

Sjekk status med:

```powershell
bildebank face-status
```

Kommandoen viser om config er av eller på, hvor modellene skal ligge, og om
`insightface` og `onnxruntime` er installert.

Hvis kommandoen kjøres fra en bildesamling, viser den også status for
ansiktsdatabasen i bildesamlingen.

## Scanne ansikter

Når InsightFace er installert og config er slått på, kan du teste scanning:

```powershell
bildebank face-scan --limit 10
```

`face-scan` scanner bare importerte bildefiler, ikke videoer. Bilder som
allerede er scannet med samme innhold hoppes over.

Ansiktsdata lagres i bildesamlingen:

```text
.bilder-faces.sqlite3
```

Dette er en egen database. Den vanlige Bildebank-databasen endres ikke.

## Rapport

Etter scanning kan du se en enkel rapport:

```powershell
bildebank face-report
```

Rapporten viser blant annet:

- antall scannede filer
- antall ansikter
- hvor mange filer som har 0, 1 eller flere ansikter
- bilder med flest ansikter
- eventuelle scan-feil

## HTML-visning

Du kan lage en enkel HTML-side for å se ansiktene som er funnet:

```powershell
bildebank make-face-browser
```

Da lages:

```text
faces.html
```

Første versjon viser bildene der ansikter er funnet, og tegner en boks rundt
hvert ansikt. Den grupperer ikke personer ennå.

## Slette ansiktsdata

Hvis du vil fjerne alle eksperimentelle ansiktsdata fra bildesamlingen:

```powershell
bildebank face-reset
```

Kommandoen sletter `.bilder-faces.sqlite3`. Den sletter ingen bilder og endrer
ikke den vanlige Bildebank-databasen.

## Modeller

InsightFace kan laste ned modeller første gang det brukes. Bildebank bruker
modellmappen fra config, slik at modellene havner i programmappen og ikke
spres andre steder.

De ferdigtrente modellene fra InsightFace er oppgitt som kun for
ikke-kommersiell forskning. Dette må avklares før funksjonen brukes til noe mer
enn lokal testing.

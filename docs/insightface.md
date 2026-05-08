# InsightFace

NB: Denne funksjonen er ikke ferdig ennå, så det er foreløpig bortkastet
for andre enn Tom Cato å teste dette. På samme måte som import av bilder
i begynnelsen bestod av flere trinn, så regner jeg med at dette forenkles
etterhvert som jeg ser at alle trinnene fungerer greit.

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

Kommandoen viser progresjon mens den jobber. Først kontrollerer den hvilke
bilder som allerede er scannet, og deretter scanner den nye eller endrede
bilder.

Bildebank skjuler vanligvis intern output fra InsightFace og ONNX Runtime. Hvis
du feilsøker selve ansiktsmodellen, kan den vises med:

```powershell
bildebank face-scan --show-model-output
```

Det er trygt å avbryte med `Ctrl-C`. Bildebank lagrer resultatet etter hvert
bilde. Neste gang du kjører `face-scan`, fortsetter den ved å hoppe over bilder
som allerede er ferdig scannet. For å finne ansikter i alle bildene må denne
kjøres på alle bildene i samlingen.

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

## Ansiktsgrupper

Du kan beregne foreløpige grupper basert på embeddingene fra InsightFace:

```powershell
bildebank face-group
```

Dette lager mulige grupper av ansikter som ligner hverandre. Gruppene er bare
forslag, ikke bekreftede personer.

Kommandoen viser progresjon mens den sammenligner ansikter. Dette kan ta tid,
fordi hvert ansikt sammenlignes med mange andre ansikter.

Du kan justere hvor strengt ansikter skal sammenlignes:

```powershell
bildebank face-group --threshold 0.65
```

Høyere tall gir strengere grupper. Standard er `0.60`.

Lag HTML-side for gruppene:

```powershell
bildebank make-face-groups-browser
```

Da lages:

```text
face-groups.html
```

Siden viser én gruppe om gangen. Bruk pil venstre og pil høyre for å bla mellom
gruppene. Bildene vises med ansiktsboks, ikke som et lite crop-utsnitt, slik at
det er lettere å se om riktig ansikt er markert.

## Personer

Når en gruppe ser riktig ut, kan du opprette en person og koble gruppen til
personen:

```powershell
bildebank face-person-create "Kari"
bildebank face-person-add-group "Kari" 3
```

Tallet er gruppe-id fra `face-groups.html`.

Personen må være opprettet før du kobler grupper eller enkeltansikter til den.
Dette hindrer at en skrivefeil i navnet lager en ny person ved et uhell.

Du kan se registrerte personer:

```powershell
bildebank face-person-list
```

Hvis en person er opprettet ved en feil, kan den slettes fra ansiktsdatabasen:

```powershell
bildebank face-person-delete "Krai"
```

Kommandoen ber om bekreftelse. Den sletter bare personen, bekreftede
ansiktskoblinger og forslag for personen. Den sletter ingen bilder og ingen
scannede ansikter.

Dette er brukerbekreftet informasjon. Det er fortsatt bare ansiktene i gruppen
som kobles til personen.

Du kan også koble ett enkelt ansikt til en person. Ansikt-id står i
`faces.html` og `face-groups.html`. Personen må være opprettet først.

```powershell
bildebank face-person-add-face "Kari" 17
```

Hvis et ansikt er koblet feil, kan koblingen fjernes igjen:

```powershell
bildebank face-person-remove-face "Kari" 17
```

Når noen ansikter er koblet til personer, kan Bildebank lage forslag for
ukjente ansikter:

```powershell
bildebank face-suggest
```

Forslagene er ikke bekreftede personer. De bygger bare på ansikter du allerede
har koblet manuelt. Kommandoen skriver ut personnavn, ansikt-id, score og fil.
Strengheten kan justeres:

```powershell
bildebank face-suggest --threshold 0.70
```

Høyere tall gir færre og strengere forslag.

For å se bildene der Bildebank mener at en person finnes:

```powershell
bildebank make-person-browser "Kari"
```

Da lages for eksempel:

```text
person-Kari.html
```

Siden viser ett bilde om gangen, slik at den også kan brukes når personen finnes
i mange bilder. Du kan bla til forrige/neste bilde, forrige/neste måned og
forrige/neste år. Når du hopper måned eller år, vises en månedsoversikt med
bilder fra den måneden.

Bekreftede ansikter og forslag har ulik farge på boksen rundt ansiktet.

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

# InsightFace

Ansiktsgjenkjenning fungerer ganske greit nå, selv om dokumentasjon
kanskje skal forbedres litt etter hvert. Spør etter det som er vanskelig.

InsightFace brukes til ansikansiktsgjenkjenning, og funksjonen er slått
av i vanlig Bildebank-installasjon. Denne siden viser hvordan dette kan
lastes ned og slås på.

## Kom i gang

### 1. Installer og slå på

Kjør dette fra programmappen:

```powershell
.\install-insightface.ps1
```

Åpne `bildebank-config.toml` og sett:

```toml
[face_recognition]
enabled = true
```

Du kan bla det frem til programmappen til Bildebank, høyreklikke på filen og velg
"Åpne i" og deretter "Notisblokk".

Sjekk at det er klart:

```powershell
bildebank face-status
```

### 2. Gå til bildesamlingen

Kjør resten fra bildesamlingen:

```powershell
cd "C:\Users\deg\Pictures\Min bildesamling"
```

Hvis du er usikker på hvor bildesamlingen ligger:

```powershell
bildebank where-is
```

### 3. Scan bildene

Test gjerne med noen få bilder først:

```powershell
bildebank face-scan --limit 100
```

Når testen ser grei ut, scan hele bildesamlingen:

```powershell
bildebank face-scan
```

Det er trygt å avbryte med `Ctrl-C`. Neste gang fortsetter Bildebank ved å
hoppe over bilder som allerede er scannet. Hvis du er utålmodig med å teste
systemet, så kan la `face-scan` scanne et par hundre bilder før du avbryter
og gjør resten av oppskriften, og så begynner du på nytt her etterpå.

Det er denne kommandoen som tar lang tid. Alle de andre går mye raskere.
Det mulig å nullstille alle ansiktene du knytter til personer
uten å måtte kjøre `face-scan` på nytt.

### 4. Opprett personer

Når du finner et tydelig ansikt som tilhører en person:

```powershell
bildebank face-person-create "Kari"
bildebank face-person-add-face "Kari" 798
```

Tallet `798` er `face-id`. Du finner det i `faces.html`, i personsidene eller
i vanlig `index.html`.

Du trenger ikke å markere alle ansikter manuelt. Det er vanligvis nok å legge
inn noen få sikre eksempler per person.

Hvis bare ett enkelt ansikt skal kobles til personen:

```powershell
bildebank face-person-add-face "Kari" 798
```

Ikke koble usikre ansikter. Noen få sikre ansikter er bedre enn mange
tvilsomme.

### 6. La Bildebank foreslå flere bilder

Når du har bekreftet noen ansikter for en person:

```powershell
bildebank face-suggest
```

`face-suggest` bruker da bildene du har identifisert manuelt til å
finne identifiserte personer i alle bildene som har blitt scannet med
`face-scan`. Kommandoen oppdaterer også `personer.html` og personsidene.

Åpne `personer.html`, og klikk på personen du vil se. Personsiden viser både
bekreftede treff og forslag.

Hvis forslagene ser riktige ut, kan du bekrefte flere ansikter:

```powershell
bildebank face-person-add-face "Kari" 912
```

Kjør deretter på nytt:

```powershell
bildebank face-suggest
```

### 7. Vedlikehold

List personer:

```powershell
bildebank face-person-list
```

Fjern ett feil ansikt fra en person:

```powershell
bildebank face-person-remove-face "Kari" 912
```

Tallet `912` er `face-id`. Du finner det under bildet i `faces.html` eller i
personsiden.

Slett en person som ble opprettet ved en feil:

```powershell
bildebank face-person-delete "Krai"
```

## Detaljer

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
bilder. Under selve scanningen viser den også et enkelt estimat for hvor mye tid
som gjenstår.

Bildebank skjuler vanligvis intern output fra InsightFace og ONNX Runtime. Hvis
du feilsøker selve ansiktsmodellen, kan den vises med:

```powershell
bildebank face-scan --show-model-output
```

Hvis en bildefil feiler under scanning, skriver `face-scan` filstien og
feilmeldingen. Du kan også se scan-feil senere med:

```powershell
bildebank face-report
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

## Ansikter og personer

`make-face-browser` lager en oversikt over scannede ansikter. Den er nyttig når
du vil finne et konkret ansikt du kjenner igjen og koble det til en person.

Når du har funnet et tydelig ansikt:

```powershell
bildebank face-person-create "Kari"
bildebank face-person-add-face "Kari" 798
```

Tallet er `face-id`. Du finner det under bildet i `faces.html`, eller i vanlig
`index.html` med knappen `Ansikter i bildet`.

Personen må være opprettet før du kobler ansikter til den. Dette hindrer at en
skrivefeil i navnet lager en ny person ved et uhell.

Du trenger ikke å bekrefte mange ansikter for hver person. Noen få sikre
eksempler er som regel nok til at `face-suggest` kan gjøre resten.

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

Dette er brukerbekreftet informasjon. Hvis et ansikt er koblet feil, kan
koblingen fjernes igjen:

```powershell
bildebank face-person-remove-face "Kari" 17
```

Tallet er `face-id`. Du finner det under bildet i `faces.html` eller i
personsiden.

Når noen ansikter er koblet til personer, kan Bildebank lage forslag for
ukjente ansikter:

```powershell
bildebank face-suggest
```

Forslagene er ikke bekreftede personer. De bygger bare på ansikter du allerede
har koblet manuelt. Kommandoen skriver ut personnavn, ansikt-id, score og fil.
Den oppdaterer også `personer.html` og personsidene automatisk.
Strengheten kan justeres:

```powershell
bildebank face-suggest --threshold 0.70
```

Høyere tall gir færre og strengere forslag.

Hvis du bare vil beregne forslag uten å skrive HTML, kan du bruke:

```powershell
bildebank face-suggest --no-browser
```

For å se bildene der Bildebank mener at registrerte personer finnes:

```powershell
bildebank make-people-browser
```

Da lages en index og én side per person:

```text
personer.html
person-Kari.html
```

Personsiden viser ett bilde om gangen, slik at den også kan brukes når personen
finnes i mange bilder. Du kan bla til forrige/neste bilde, forrige/neste måned
og forrige/neste år. Når du hopper måned eller år, vises en månedsoversikt med
bilder fra den måneden.

Bekreftede ansikter og forslag har ulik farge på boksen rundt ansiktet.

## Slette ansiktsdata

`face-reset` krever alltid bekreftelse før noe slettes.

Det finnes tre nivåer.

### Slette alt

Hvis du vil fjerne alle ansiktsdata fra bildesamlingen:

```powershell
bildebank face-reset --all
```

Dette sletter `.bilder-faces.sqlite3`. Det fjerner resultatene fra `face-scan`,
personer, bekreftede ansiktskoblinger og forslag.

Kommandoen sletter ingen bilder og endrer ikke den vanlige Bildebank-databasen.

### Beholde face-scan

Hvis du vil slippe å scanne bildene på nytt, men vil starte på nytt med
personer:

```powershell
bildebank face-reset --keep-scan
```

Dette beholder resultatene fra `face-scan`, men sletter personer,
bekreftede ansiktskoblinger og forslag.

Hvis du kjører `face-reset` uten nivåvalg, er dette standardnivået:

```powershell
bildebank face-reset
```

## Modeller

InsightFace kan laste ned modeller første gang det brukes. Bildebank bruker
modellmappen fra config, slik at modellene havner i programmappen og ikke
spres andre steder.

De ferdigtrente modellene fra InsightFace er oppgitt som kun for
ikke-kommersiell forskning. Dette må avklares før funksjonen brukes til noe mer
enn lokal testing.

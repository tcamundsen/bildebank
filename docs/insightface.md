# InsightFace

Ansiktsgjenkjenning fungerer ganske greit nå, selv om dokumentasjon
kanskje skal forbedres litt etter hvert. Spør etter det som er vanskelig.
Når du har lært hvordan ting gjøres, så bør du se på filen
[face-suggest-strategier](face-suggest-strategier.md).
Oversikt over kommandoer som har med ansiktsgjenkjenning å gjøre
[finner du her](reference.md#ansiktsgjenkjenning).


InsightFace brukes til ansiktsgjenkjenning, og funksjonen er slått
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

Start serveren og åpne bildebrowseren i nettleseren:
```powershell
bildebank runserver
```

Når InsightFace finner bilder med ansikter vises knappen "Ansikter i bildet"
øverst i nettleservinduet.

Trykk på den, og vi får opp visningen der vi kan legge til personer
og knytte ansikter til personer. For hvert ansikt funnet i bildet
vil visningen vise:

- en linje med teksten face-id 0000, deteksjon 0.xyz
- bildet, med funnet ansikt markert med et rødt rektangel
- en rad med knapper med navnene på de personene som allerede er definert
- en linje med teksten "Ny person", et tekstfelt til å skrive i og en
  knapp med teksten "Identifiser"

Hvis ansiktet markert med rødt rektangel er en allerede definert person,
så klikker du på knappen med den personen. Hvis du vil legge til en ny
person, så skriver du navnet og trykker "Identifiser".

Hvis det er mange personer i bildet, så vil du se et bilde med rødt
rektangel rundt ansiktet for hver eneste person. Pass på at du bruker
knappene under bildet der riktig person er markert.


```powershell
bildebank face-person-create "Kari"
bildebank face-person-add-face "Kari" 798
```

Tallet `798` i eksempelet over er `face-id` som står over bildet.

Du trenger ikke å markere alle ansikter manuelt. Det er vanligvis nok å legge
inn noen få sikre eksempler per person. Du får best resultat med bilder som er
i fokus og med god oppløsning.

Ikke koble usikre ansikter eller dårlige bilder. Noen få sikre ansikter er
bedre enn mange tvilsomme.

### 6. La Bildebank foreslå flere bilder

Når du har bekreftet noen ansikter for en person:

```powershell
bildebank face-suggest
```

`face-suggest` bruker da bildene du har identifisert manuelt til å
finne identifiserte personer i alle bildene som har blitt scannet med
`face-scan`.

I bildebrowseren fra `run-server` ser man øverst på skjermen knapper med
navnet til personer som `face-suggest` har kjent igjen. Og det er en knapp
"Personer" som tar deg til en side med lenker til bildebrowser som bare viser
bilder med personen du velger. Knappen "Bekreftede bilder" viser bare bildene
du har bekreftet er personen.

Hvis du bekrefter flere personer, så må du kjøre face-suggest på nytt:

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

Slett en person som ble opprettet ved en feil, eller for å starte på nytt med
den personen:

```powershell
bildebank face-person-delete "Kari"
```

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

## Statiske HTML-filer

Du kan lage en statisk bildebrowsere som viser alle bildene med en person
slik:

```
bildebank make-person-browser "Tom"
```

og statisk bildebrowser av alle personer, samt oversiktsfilen `personer.html`
lik:

```powershell
bildebank make-people-browser
```

## Slette ansiktsdata

Se [`face-reset`](face-reset.md).

## Modeller

InsightFace kan laste ned modeller første gang det brukes. Bildebank bruker
modellmappen fra config, slik at modellene havner i programmappen og ikke
spres andre steder.

De ferdigtrente modellene fra InsightFace er oppgitt som kun for
ikke-kommersiell forskning. Dette må avklares før funksjonen brukes til noe mer
enn lokal testing.

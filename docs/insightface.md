# InsightFace

Denne siden viser hvordan du slår på ansiktsgjenkjenning i Bildebank,
scanner bildesamlingen, knytter sikre ansikter til personer og lar Bildebank
foreslå flere bilder av de samme personene.

InsightFace-funksjonen er slått av i vanlig Bildebank-installasjon. Når den er
slått på, lagres ansiktsdata i Bildebank-databasen. Selve bildefilene endres
ikke.

Merk: De ferdigtrente modellene fra InsightFace er oppgitt som kun for
ikke-kommersiell forskning. Avklar lisensvilkår før funksjonen brukes til
annet enn lokal testing.

## Kom i gang

Arbeidet består av tre hovedtrinn:

1. [`face-scan`](face-scan.md) finner ansikter i bildene.
2. Du knytter noen sikre ansikter manuelt til personer.
3. [`face-suggest`](face-suggest.md) bruker de sikre eksemplene til å foreslå flere bilder.

### Installer og slå på

Kjør dette fra programmappen til Bildebank:

```powershell
.\install-insightface.ps1
```

Slå på ansiktsgjenkjenning:

```powershell
bildebank config face_recognition enable
```

Sjekk at funksjonen er klar:

```powershell
bildebank face-status
```

### Gå til bildesamlingen

Kjør resten av kommandoene fra bildesamlingen:

```powershell
cd "C:\Users\deg\Pictures\Min bildesamling"
```

Hvis du er usikker på hvor bildesamlingen ligger:

```powershell
bildebank where-is
```

### Scan bildene

Test gjerne med noen få bilder først:

```powershell
bildebank face-scan --limit 100
```

Når testen ser grei ut, kan du scanne hele bildesamlingen:

```powershell
bildebank face-scan
```

Det er trygt å avbryte med `Ctrl-C`. Neste gang fortsetter Bildebank ved å
hoppe over bilder som allerede er scannet.

Hvis du vil teste resten av systemet før hele samlingen er ferdig scannet, kan
du la `face-scan` scanne noen hundre bilder, avbryte med `Ctrl-C` og fortsette
med resten av oppskriften. Senere kan du kjøre `face-scan` på nytt for å
fullføre scanningen.

Dette er kommandoen som tar lang tid. De andre kommandoene går vanligvis mye
raskere.

Du kan nullstille ansiktene du har knyttet til personer uten å scanne
bildesamlingen på nytt.

### Knytt ansikter til personer

Start serveren og åpne bildebrowseren i nettleseren:

```powershell
bildebank run-server
```

Når et bilde har scannede ansikter, vises knappen **Ansikter i bildet** øverst
i nettleservinduet. Trykk på den for å åpne ansiktsvisningen. For hvert ansikt
i bildet viser Bildebank:

- en linje med teksten `face-id 0000`, deteksjon `0.xyz`
- bildet, med funnet ansikt markert med et rødt rektangel
- en rad med knapper med navnene på personer som allerede er opprettet
- en linje med teksten "Ny person", et tekstfelt og en knapp med teksten
  **Identifiser**

Hvis ansiktet i det røde rektangelet er en person som allerede er opprettet,
klikker du på knappen med riktig navn. Hvis du vil opprette en ny person,
skriver du navnet i tekstfeltet og trykker **Identifiser**.

Hvis det er mange personer i bildet, vises ett utsnitt for hvert ansikt. Pass
på at du bruker knappene under utsnittet der riktig ansikt er markert.

Du trenger ikke å markere alle ansikter manuelt. Det er vanligvis nok å legge
inn noen få sikre eksempler per person. Du får best resultat med bilder som er
i fokus og har god oppløsning.

Ikke koble usikre ansikter eller dårlige bilder. Noen få sikre ansikter er
bedre enn mange tvilsomme.

### La Bildebank foreslå flere bilder

Når du har bekreftet noen sikre ansikter for en person, kan du kjøre:

```powershell
bildebank face-suggest  --threshold 0.45
```

[`face-suggest`](face-suggest.md) sammenligner de bekreftede ansiktene med
andre scannede ansikter og lagrer forslag til hvilke bilder som kan vise samme
person. Du kan angi et tall mellom 0.0 og 1.0 med `--threshold` som avgjør
hvor like bildene må være før programmet antar det er samme person. Du
må eksperimentere med dine bilder for å se hvilken tallverdi som passer
for dine bilder.

Forslagene er ikke det samme som manuell bekreftelse. Bruk sikre, tydelige
bilder som grunnlag, og ikke la dårlige forslag bli nye sikre eksempler.

Etter at du har kjørt `face-suggest`, vises forslagene i bildebrowseren fra
`run-server`:

- Øverst på skjermen vises knapper med navn på personer som Bildebank har
  forslag for.
- Knappen "Personer" åpner en side med lenker til bildebrowser for hver
  person.
- Knappen "Bekreftede bilder" viser bare bildene der du selv har bekreftet
  personen.

Hvis du bekrefter flere ansikter senere, må du kjøre `face-suggest` på nytt for
å oppdatere forslagene.  Når grunnflyten er kjent, kan du lese mer i
[face-suggest-strategier](face-suggest-strategier.md).

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

## Vedlikehold

De vanligste vedlikeholdsoppgavene er:

- liste personer: `bildebank face-person-list` eller klikk **Personer** i nettleseren.
- fjerne et feil ansikt fra en person: `bildebank face-person-remove-face` eller klikk
  **Personer**, deretter **Bekreftede bilder** på raden for personen, bla til bildet
  og klikk **Avkreft face-id nnnn**
- endre navn på en person: `bildebank face-person-rename` eller klikk **Personer**
  og deretter **endre navn** bak navnet som skal endres.
- slette en person: `bildebank face-person-delete`
- nullstille ansiktskoblinger: se [`face-reset`](face-reset.md)

Se kommandosidene for detaljer og eksempler.

## Statiske HTML-filer

Du kan lage en statisk bildebrowser som viser alle bildene med en person med
kommandoen [`make-person-browser`](make-person-browser.md):

```powershell
bildebank make-person-browser "Tom"
```

Du kan også lage statiske bildebrowsere for alle personer, sammen med
oversiktsfilen `personer.html` med kommandoen
[`make-people-browser`](make-people-browser.md):

```powershell
bildebank make-people-browser
```

## Mer informasjon

- [Oversikt over kommandoer for ansiktsgjenkjenning](reference.md#ansiktsgjenkjenning)
- [Strategier for `face-suggest`](face-suggest-strategier.md)

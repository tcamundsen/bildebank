## Dette kan nå også gjøres i nettleser når du bruker `run-server`

Selv om teksten nedenfor viser
hvordan du jobber i PowerShell, så velger du bilder etter samme
prinsipper.

Jeg jobber nå med å finjustere ting, og skal oppdatere dokumentasjonen
når jeg har mer oversikt. Men dette er finn jeg har gjort til nå:

- Du kommer langt med et babybilde, et som 3-åring, et som 6-åring, som 18-åring
  og et som mer voksen, for å finne alle bilder av en person.
- Du bør bekrefte bilder av alle søsken i en søskenflokk, ellers kan
  programmet ta feil basert på søskenlikhet.
- Generelt kan man vel si at desto flere personer som har et bekreftet
  bilde, desto mindre sjanse er det for feilidentifikasjoner.

## Bruk tydelige, gode bilder

Tydelige bilder i fokus, tatt forfra fungerer best.  Uklare, mørke bilder og
bilder i lav oppløsning kan gjøre resultatene fra `face-suggest` dårligere.


```powershell
bildebank face-person-add-face "Kari" 798
```

`face-id` finner du i `faces.html`, i personsidene, eller i vanlig `index.html`
med knappen `Ansikter i bildet`.

## Gi variasjon

`face-suggest` blir bedre når eksemplene viser personen i litt ulike
situasjoner.

Prøv å bekrefte noen ansikter fra:

- ulike år
- ulik alder
- både inne og ute
- ulike vinkler
- ulike lysforhold
- briller og uten briller, hvis det er relevant

For barn kan alder være ekstra viktig. Et barn kan se ganske annerledes ut
etter noen år. Da kan det hjelpe å bekrefte noen ansikter fra flere perioder.

## Kjør face-suggest flere ganger

Et godt mønster er:

```powershell
bildebank face-suggest
```

Se gjennom personsidene eller vanlig bildebrowser. Når du finner sikre treff
som ikke er bekreftet ennå, legg til noen av dem:

```powershell
bildebank face-person-add-face "Kari" 912
```

Kjør deretter:

```powershell
bildebank face-suggest
```

Hver gang du legger til gode eksempler, får `face-suggest` bedre grunnlag for
neste runde.

## Bruk vanlig bildebrowser når du oppdager feil

Når du blar i vanlig `index.html`, kan du se et bilde og tenke at Bildebank
burde kjent igjen en person.

Da kan du bruke knappen:

```text
Ansikter i bildet
```

Den viser hvert scannede ansikt i bildet med `face-id` og en kommando du kan
kopiere:

```powershell
bildebank face-person-add-face "Navn" 1234
```

Bytt ut `Navn` med riktig personnavn før du kjører kommandoen. Kjør deretter
`face-suggest` på nytt.

## Ikke stress med alt

Du trenger ikke å bekrefte hvert eneste ansikt. Målet er å gi Bildebank nok
gode eksempler til at `face-suggest` kan gjøre mye av arbeidet selv.

## Se status underveis

Bruk:

```powershell
bildebank face-person-list
```

Den viser hvor mange bilder du har bekreftet for hver person, og hvor mange
forslag `face-suggest` har laget.

Bruk også:

```powershell
bildebank face-report
```

Den gir oversikt over hvor mange bilder som har bekreftede personer, og hvor
mange bilder som fortsatt har ukjente ansikter.

## Hvis forslagene blir dårlige

Hvis en person får mange dårlige forslag, sjekk først om du har koblet feil
ansikt til personen.

Du kan fjerne et feil ansikt:

```powershell
bildebank face-person-remove-face "Kari" 912
```

Kjør deretter:

```powershell
bildebank face-suggest
```

Hvis du er veldig usikker på dataene, kan du nullstille personkoblinger og
forslag uten å slette scanningen:

```powershell
bildebank face-reset
```

Standardvalget beholder resultatene fra `face-scan`, slik at du slipper å
scanne alle bildene på nytt.

## Praktisk anbefaling

For hver viktig person:

1. Finn 2-3 sikre ansikter i `faces.html`, personsidene eller vanlig `index.html`.
2. Legg dem til med `face-person-add-face`.
3. Kjør `face-suggest`.
4. Se gjennom personsiden i `personer.html`.
5. Legg til noen flere sikre enkeltansikter hvis det mangler gode eksempler.
6. Kjør `face-suggest` på nytt.

Når forslagene ser gode ut, trenger du ikke å gjøre mer med den personen før du
oppdager konkrete bilder som mangler.

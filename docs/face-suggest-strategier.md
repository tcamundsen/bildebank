# Strategier for face-suggest

Denne siden handler om hvordan du kan hjelpe `face-suggest` med å kjenne igjen
flest mulig ansikter.

Kortversjonen er: gi Bildebank noen få, gode og varierte eksempler på hver
person. Ikke prøv å bekrefte alt manuelt.

## Start med sikre ansikter

Begynn med ansikter du er helt sikker på. Hvis du kobler feil person til et
ansikt, lærer `face-suggest` av feilen og kan lage dårligere forslag senere.

Gode eksempler er bilder der:

- ansiktet er tydelig
- personen ser omtrent mot kameraet
- ansiktet ikke er veldig uskarpt
- det ikke er tvil om hvem personen er

Bruk heller fem sikre ansikter enn femti ansikter der noen kan være feil.

## Bruk grupper først

Kjør:

```powershell
bildebank face-group
```

Åpne `face-groups.html`. Når en gruppe tydelig viser samme person i alle
synlige bilder, kan du koble gruppen til personen:

```powershell
bildebank face-person-add-group "Kari" 12
```

Tallet er gruppe-id fra `face-groups.html`.

Dette er den raskeste måten å gi Bildebank mange gode eksempler på.

## Ikke godkjenn svake grupper

Ikke bruk `face-person-add-group` hvis gruppen inneholder forskjellige
personer. Da er det bedre å la gruppen være.

Hvis bare noen få bilder i gruppen er riktige, bruk heller enkeltansikter:

```powershell
bildebank face-person-add-face "Kari" 798
```

`face-id` finner du i `face-groups.html`, i personsidene, eller i vanlig
`index.html` med knappen `Ansikter i bildet`.

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

## Ikke stress med alle grupper

Det er normalt at `face-group` lager mange grupper. Du trenger ikke å koble
hver gruppe til en person.

Målet er å gi Bildebank nok gode eksempler til at `face-suggest` kan gjøre mye
av resten av arbeidet.

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

1. Finn 2-3 gode grupper i `face-groups.html`.
2. Legg til gruppene med `face-person-add-group`.
3. Kjør `face-suggest`.
4. Se gjennom personsiden i `personer.html`.
5. Legg til noen sikre enkeltansikter som mangler.
6. Kjør `face-suggest` på nytt.

Når forslagene ser gode ut, trenger du ikke å gjøre mer med den personen før du
oppdager konkrete bilder som mangler.

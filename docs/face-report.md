# face-report
<!-- CLI-HELP-START -->
```text
usage: bildebank face-report [valg]

options:
  -h, --help     show this help message and exit
  --limit LIMIT  Maks antall linjer per liste
```
<!-- CLI-HELP-END -->

`face-report` viser rapport for scannede ansikter.

## Scannede bilder

Disse tallene handler om hva `face-scan` har funnet:

- `Scannede filer` er antall bildefiler som er kontrollert av `face-scan`.
- `Ansikter funnet` er antall ansikter som er funnet i disse bildene.
- `Bilder uten ansikter` er scannede bilder der Bildebank ikke fant ansikter.
- `Bilder med ett ansikt` er scannede bilder der Bildebank fant nøyaktig ett ansikt.
- `Bilder med flere ansikter` er scannede bilder der Bildebank fant to eller flere ansikter.
- `Scan-feil` er bilder Bildebank ikke klarte å lese eller scanne.

Merk forskjellen på bilder og ansikter. Ett bilde med fire personer teller som
ett bilde, men fire ansikter.

## Personstatus

Disse tallene handler om personer du har opprettet, bekreftelser du har gjort,
og forslag Bildebank har laget etterpå:

- `Personer` er antall personer som er opprettet med `face-person-create`.
- `Bekreftede ansiktskoblinger` er antall ansikter du selv har koblet til en
  person. Dette skjer med `face-person-add-face`.
- `Forslag` er antall ukjente ansikter som `face-suggest` mener kan være en
  kjent person. Forslag er ikke bekreftet av deg.
- `Bilder med minst én bekreftet person` er antall bildefiler der minst ett
  ansikt er bekreftet koblet til en person. Hvis ett bilde har tre bekreftede
  personer, teller bildet fortsatt bare som ett bilde her.
- `Bilder med ansikter, men ingen bekreftet person` er scannede bilder der
  Bildebank har funnet ansikter, men ingen av ansiktene i bildet er bekreftet
  koblet til en person.
- `Bilder med både bekreftede og ukjente ansikter` er bilder der minst ett
  ansikt er bekreftet, samtidig som minst ett annet ansikt i samme bilde
  fortsatt er ukjent.

`Forslag` teller ansikter, ikke bilder. Hvis ett bilde har tre ansikter som
`face-suggest` foreslår navn på, teller det som tre forslag.

## Bilder med flest ansikter

Rapporten viser også en liste over bilder med flest ansikter. Dette er nyttig
for å finne bilder der det kan være mange personer å kontrollere.

`--limit` bestemmer hvor mange slike linjer rapporten viser:

```powershell
bildebank face-report --limit 50
```

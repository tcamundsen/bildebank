# face-reset
<!-- CLI-HELP-START -->
```text
usage: bildebank face-reset [valg]

Sletter ansiktsdata på valgt nivå. Kommandoen krever alltid nøyaktig
bekreftelse før noe slettes.

options:
  -h, --help   show this help message and exit
  --all        Slett hele face-databasen, inkludert face-scan, personer og
               forslag.
  --keep-scan  Behold face-scan-resultater, men slett personer, bekreftelser
               og forslag. Standard hvis ingen nivåvalg er brukt.
```
<!-- CLI-HELP-END -->

`face-reset` sletter ansiktsdata.

Kommandoen krever alltid bekreftelse.

Uten nivåvalg er standard det samme som `--keep-scan`.
Det er dette som er befalt hvis du vil starte på nytt med å
finne personer, for da slipper du å kjøre `face-scan` på nytt.

Kommandoen sletter ingen bilder og endrer ikke den vanlige Bildebank-databasen.

# Valg

## `--keep-scan`

Behold resultatene fra `face-scan`, men sletter personer, bekreftelser og
forslag.

## `--all`

Sletter hele face-databasen. Bruk dette bare hvis du også vil slette
resultatene fra `face-scan`. Dette er vanligvis ikke nødvendig med mindre
du vil slutte å bruke ansiktsgjenkjenning.


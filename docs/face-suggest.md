# face-suggest
<!-- CLI-HELP-START -->
```text
usage: bildebank face-suggest [valg]

Foreslå personer for ukjente ansikter.

options:
  -h, --help            show this help message and exit
  --threshold THRESHOLD
                        Likhetsterskel fra 0.0 til 1.0. Standard: 0.6
  --model NAVN          Bruk face-database for denne InsightFace-modellen uten
                        å endre config-filen
```
<!-- CLI-HELP-END -->

`face-suggest` foreslår personer for ukjente ansikter.

Forslagene bygger på ansikter du allerede har bekreftet med
`face-person-add-face`. Hvert ukjent ansikt sammenlignes med hvert bekreftet
ansikt. Hvis beste treff er likt nok, foreslås personen som det bekreftede
ansiktet hører til.

Manuell **Person i bildet** fra `run-server` brukes bare til å vise at en
eksisterende person er i bildet. Den bekrefter ikke et bestemt ansikt og brukes
ikke av `face-suggest`.

Se også [`Strategier for face-suggest`](face-suggest-strategier.md) for råd om
hvordan du bør velge ansikter som skal bekreftes.

Du trenger ikke å restarter `run-server` etter å ha kjørt `face-suggest`. Forslagene
brukes så snart du blar videre til neste bilde eller laster siden på nytt med Ctrl-R eller F5.

## Valg

### `--threshold DESIMALTALL`

Høyere `--threshold` gir strengere forslag. Standardverdien 0.6 er basert på et forslag
fra AI. Men det kan se ut som at helt ned mot 0.4 kan fungere.

### `--model NAVN`

Bruker face-databasen for en annen InsightFace-modell enn modellen som er valgt
i `bildebank-config.toml`. Dette endrer ikke config-filen.

# face-suggest
<!-- CLI-HELP-START -->
```text
usage: bildebank face-suggest [valg]

Foreslå personer for ukjente ansikter

options:
  -h, --help            show this help message and exit
  --threshold THRESHOLD
                        Likhetsterskel fra 0.0 til 1.0. Standard: 0.6
```
<!-- CLI-HELP-END -->

`face-suggest` foreslår personer for ukjente ansikter.

Forslagene bygger på ansikter du allerede har bekreftet med
`face-person-add-face`.

## Valg

`--threshold DESIMALTALL`
: Høyere `--threshold` gir strengere forslag.

Se også [`Strategier for face-suggest`](face-suggest-strategier.md) for råd om
hvordan du bør velge ansikter som skal bekreftes.

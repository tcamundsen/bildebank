# refresh-metadata
<!-- CLI-HELP-START -->
```text
usage: bildebank refresh-metadata [valg]

options:
  -h, --help  show this help message and exit
  --dry-run   Vis oppsummering uten å flytte filer eller endre databasen
  --verbose   Vis filer som flyttes, hoppes over eller feiler
```
<!-- CLI-HELP-END -->

`refresh-metadata` sjekker filer uten metadata på nytt.

Hvis Bildebank senere klarer å lese metadata som manglet da filen ble
importert, vil denne kommandoen flytte filen til riktig datomappe.

Start med `--dry-run` for å se hva som ville skjedd uten å endre filer eller
database.


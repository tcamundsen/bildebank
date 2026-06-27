# vacuum
<!-- CLI-HELP-START -->
```text
usage: bildebank vacuum [valg]

Kjører SQLite VACUUM på Bildebank-databasene. Kommandoen endrer ikke
bildefiler.

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`vacuum` pakker Bildebank-databasene slik at SQLite-filene kan krympe fysisk.
Kommandoen tar hoveddatabasen, bildesøkdatabasen og ansiktsdatabasene som
finnes i bildesamlingen.

Kommandoen endrer ikke bildefilene.

```powershell
bildebank vacuum
```

Dette kan være nyttig etter migreringer eller andre databaseendringer som
frigjør mye plass inne i databasene.

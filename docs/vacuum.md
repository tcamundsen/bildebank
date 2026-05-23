# vacuum
<!-- CLI-HELP-START -->
```text
usage: bildebank vacuum [valg]

Kjører SQLite VACUUM på Bildebank-databasen. Kommandoen endrer ikke
bildefiler.

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`vacuum` pakker Bildebank-databasen slik at SQLite-filen kan krympe fysisk.

Kommandoen endrer ikke bildefilene.

```powershell
bildebank vacuum
```

Dette kan være nyttig etter migreringer eller andre databaseendringer som
frigjør mye plass inne i databasen.

# refresh-metadata
<!-- CLI-HELP-START -->
```text
usage: bildebank refresh-metadata [valg]

options:
  -h, --help  show this help message and exit
  --dry-run   Vis oppsummering uten å flytte filer eller endre databasen
  --rescan    Les metadata på nytt for alle aktive filer
  --verbose   Vis filer som flyttes, hoppes over eller feiler
```
<!-- CLI-HELP-END -->

`refresh-metadata` sjekker filer uten metadata på nytt.

Hvis Bildebank senere klarer å lese metadata som manglet da filen ble
importert, vil denne kommandoen flytte filen til riktig datomappe.

Hvis en aktiv databaseført fil mangler på disk, rapporterer kommandoen feil.
Den leter ikke etter en erstatningsfil andre steder i bildesamlingen.

Bruk `--rescan` for å lese metadata på nytt for alle aktive filer. Dette kan
brukes etter en databaseoppgradering som har lagt til nye metadatafelt, for
eksempel kameradata.

Kommandoen lagrer databaseendringer underveis. Hvis du avbryter med Ctrl+C,
blir metadata som allerede er behandlet lagret så langt det var mulig.

Kommandoen låser bildesamlingen mens den arbeider, fordi filer kan bli flyttet.
Andre kommandoer som endrer samlingen må vente til den er ferdig.

Start med `--dry-run` for å se hva som ville skjedd uten å endre filer eller
database.

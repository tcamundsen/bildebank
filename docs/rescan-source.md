# rescan-source
<!-- CLI-HELP-START -->
```text
usage: bildebank rescan-source [valg] --name navn

Scanner en tidligere importert kilde på nytt med dagens støttede filtyper.
Kommandoen oppretter ikke en ny kilde og sletter ingenting.

options:
  -h, --help   show this help message and exit
  --name NAME  Navn på importen som skal scannes på nytt
  --dry-run    Vis importoppsummering uten å kopiere filer eller endre
               databasen
  --quiet      Ikke vis fremdrift under scanning
```
<!-- CLI-HELP-END -->

`rescan-source` scanner en kilde som allerede er importert.

Dette er nyttig når Bildebank senere har lært å kjenne igjen flere filtyper.
Hvis du for eksempel importerte en mappe før Bildebank støttet RAW/NEF eller
PSD-filer, kan slike filer ha blitt ignorert ved første import. Da kan du
scanne samme kilde på nytt:

```powershell
bildebank rescan-source --name "Familie-CD-2004"
```

Kommandoen bruker den eksisterende kilden i databasen. Det opprettes ikke en ny
import, og filer som allerede er registrert for kilden hoppes over.

Hvis Bildebank finner en ny fil som allerede finnes i bildesamlingen fra en
annen kilde, kopieres den ikke på nytt. Bildebank registrerer bare at denne
kilden også hadde filen.

Hvis Bildebank finner en ny unik fil, kopieres den inn på samme måte som ved
vanlig `import`.

## Prøv først

Start gjerne med `--dry-run`:

```powershell
bildebank rescan-source --name "Familie-CD-2004" --dry-run
```

Da får du en oppsummering uten at filer kopieres eller databasen endres.

## Finne kildenavn

Bruk `list-sources` hvis du er usikker på navnet:

```powershell
bildebank list-sources
```

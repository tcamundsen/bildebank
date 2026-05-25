# check-source
<!-- CLI-HELP-START -->
```text
usage: bildebank check-source [valg] mappe

Scanner en kildemappe og kontrollerer at alle støttede bilde- og videofiler
finnes i bildesamlingen med samme SHA-256. Kommandoen sletter ingenting.

positional arguments:
  mappe       Kildemappen som skal kontrolleres

options:
  -h, --help  show this help message and exit
  --quiet     Ikke vis fremdrift under kontrollen
```
<!-- CLI-HELP-END -->

`check-source` kontrollerer om en gammel kildemappe kan ryddes bort manuelt.
Kommandoen scanner mappen på nytt, regner ut SHA-256 for hver støttede bilde- og
videofil, og sjekker at samme innhold finnes i bildesamlingen.

Bildebank sletter aldri kildemappen. Hvis kontrollen er trygg, viser kommandoen
en PowerShell-kommando du kan velge å kjøre selv.

Eksempel:

```powershell
bildebank check-source "C:\Users\Tom\Pictures\Gamle bilder"
```

Hvis alt er dekket, får du en oppsummering og en linje som ligner:

```powershell
Remove-Item -LiteralPath 'C:\Users\Tom\Pictures\Gamle bilder'
```

Les linjen før du bruker den. Det er du som sletter mappen, ikke Bildebank. Hvis
mappen inneholder filer, spør PowerShell før den sletter.

Hvis Bildebank finner filer som mangler, eller hvis en målfil i bildesamlingen
ikke kan valideres med SHA-256, skriver kommandoen at det ikke er trygt å slette
kildemappen ennå.

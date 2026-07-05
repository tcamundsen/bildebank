# check-source
<!-- CLI-HELP-START -->
```text
usage: bildebank check-source [valg] mappe

Scanner en kildemappe og kontrollerer at alle filer i kildemappen finnes i
bildesamlingen med samme SHA-256. Kommandoen sletter ingenting.

positional arguments:
  mappe             Kildemappen som skal kontrolleres

options:
  -h, --help        show this help message and exit
  --quiet           Ikke vis fremdrift under kontrollen
  --accept-deleted  Godta filer som finnes i bildesamlingens deleted/-mappe
```
<!-- CLI-HELP-END -->

`check-source` kontrollerer om en gammel kildemappe kan ryddes bort manuelt.
Kommandoen scanner mappen på nytt, regner ut SHA-256 for alle filer i
kildemappen, og sjekker at samme innhold finnes i bildesamlingen.

Bildebank sletter aldri kildemappen. Hvis kontrollen er trygg, viser kommandoen
en PowerShell-kommando du kan velge å kjøre selv.

Eksempel:

```powershell
bildebank check-source "C:\Users\Tom\Pictures\Gamle bilder"
```

Hvis kildemappen kommer fra Google Takeout, ignorerer `check-source`
JSON-sidecarfiler som hører til en bilde- eller videofil i samme mappe, for
eksempel `IMG_20240102.jpg.json`. Andre `.json`-filer rapporteres fortsatt som
problemfiler.

Hvis alt er dekket, får du en oppsummering og en linje som ligner:

```powershell
Remove-Item -LiteralPath 'C:\Users\Tom\Pictures\Gamle bilder'
```

Les linjen før du bruker den. Det er du som sletter mappen, ikke Bildebank. Hvis
mappen inneholder filer, spør PowerShell før den sletter. Du kan markere teksten
fra kommandoen, trykke Ctrl-C for å kopiere, og så Ctrl-V for å lime inn.

Hvis Bildebank finner filer som ikke er importert i bildesamlingen, eller hvis
en fil i bildesamlingen ikke kan valideres med SHA-256, skriver kommandoen at
kildemappen ikke er trygg å slette.

Hvis det finnes problemfiler, lagrer kommandoen listen med filnavn i en
midlertidig tekstfil. På Windows åpnes listen i Notepad når kommandoen er
ferdig. På Linux åpnes den med gvim.

Filer som er slettet med `bildebank remove` ligger i `deleted/`. Som standard
regnes ikke disse som trygge aktive kopier, og `check-source` viser dem som
problem merket med `[deleted/]`, både i terminalen og i tekstfilen som åpnes.

Hvis du vet at slettingen er riktig og vil godta slike filer under kontrollen,
kan du bruke:

```powershell
bildebank check-source --accept-deleted "C:\Users\Tom\Pictures\Gamle bilder"
```

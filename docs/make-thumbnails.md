# make-thumbnails
<!-- CLI-HELP-START -->
```text
usage: bildebank make-thumbnails [valg]

options:
  -h, --help     show this help message and exit
  --limit LIMIT  Maks antall bildefiler som skal sjekkes
  --verbose      Vis filer som feiler
```
<!-- CLI-HELP-END -->

`make-thumbnails` lager små bildefiler som brukes i månedsvisningen.

Kommandoen går gjennom de importerte bildene i bildesamlingen og lager
thumbnail-filer i mappen `thumbs`.

Eksempel:

```text
C:\Bilder\Samling\2012\10\image1.jpg
C:\Bilder\Samling\thumbs\2012\10\image1.jpg
```

Hvis originalen er PNG, WEBP eller HEIC/HEIF, får thumbnail-filen likevel
filendelsen `.jpg`.

Kommandoen endrer ikke originalbildene. Hvis en thumbnail allerede finnes og er
nyere enn originalbildet, blir den ikke laget på nytt.

## Når bør du kjøre den?

Kjør kommandoen før `make-browser` eller før du bruker `run-server` hvis
månedsvisningen er treg med store bilder.

```powershell
bildebank make-thumbnails
bildebank make-browser
```

Du kan også kjøre den etter en ny import. Da lager Bildebank thumbnails for nye
bilder og hopper over thumbnails som fortsatt er oppdatert.

Kommandoen tåler fint å avbrytes med Ctrl-C.


## Valg

### `--limit ANTALL`

Lag thumbnails for opptil ANTALL bilder.
Dette er nyttig hvis du vil teste kommandoen på en liten del av samlingen først.

```powershell
bildebank make-thumbnails --limit 500
```

### `--verbose`

Vis hvilke filer som feilet.

## Feilhåndtering

Hvis én bildefil er korrupt eller ikke kan åpnes, fortsetter kommandoen med
neste bilde. Til slutt viser den hvor mange feil som oppstod.

Hvis det oppstod feil for én eller flere filer, avslutter kommandoen med
exit-code `2`.

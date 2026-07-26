# make-thumbnails
<!-- CLI-HELP-START -->
```text
usage: bildebank make-thumbnails [valg]

Lag thumbnails for månedsvisning

options:
  -h, --help     show this help message and exit
  --limit LIMIT  Maks antall bildefiler som skal sjekkes
  --verbose      Vis filer som feiler
```
<!-- CLI-HELP-END -->

`make-thumbnails` lager små bildefiler som brukes i månedsvisningen. Dette
gjør at månedsvisningen laster mye lettere.

Jobben som `make-thumbnails` gjør kan også gjøres direkte fra Bildebank-vinduet.

Kommandoen går gjennom de importerte bildene i bildesamlingen og lager
thumbnail-filer i mappen `thumbs\v2`.

Eksempel:

```text
C:\Bilder\Samling\2012\10\image1.jpg
C:\Bilder\Samling\thumbs\v2\2012\10\<maskinlaget navn>.jpg
```

Thumbnailen får et fast maskinlaget navn fra originalens plassering i
bildesamlingen. Dermed kan for eksempel `image.jpg`, `image.png` og `image.webp`
aldri overskrive hverandres thumbnails. Thumbnail-filen får alltid filendelsen
`.jpg`.

Kommandoen endrer ikke originalbildene. Hvis en thumbnail allerede finnes og er
nyere enn originalbildet, er en vanlig fil uten lenker og ser ut som en komplett
JPEG-fil, blir den ikke laget på nytt.

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

Bildet må ligge på sin databaseførte plass som en vanlig fil uten symlinker,
junctions eller andre Windows reparse points. Bildebank avviser også en
`thumbs`-mappe som er en slik lenke. Bilder over 80 millioner piksler avvises for
å hindre at en skadet bildefil bruker opp minnet.

Hvis det oppstod feil for én eller flere filer, avslutter kommandoen med
exit-code `2`.

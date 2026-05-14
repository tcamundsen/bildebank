# make-thumbnails

`make-thumbnails` lager små bildefiler som brukes i månedsvisningen.

## Referanse

```powershell
bildebank make-thumbnails [valg]
```

Vanlige valg:

```powershell
bildebank make-thumbnails
bildebank make-thumbnails --limit 500
```

## Hva kommandoen gjør

Kommandoen går gjennom importerte bildefiler i bildesamlingen og lager
thumbnail-filer i mappen `thumbs`.

Eksempel:

```text
C:\Bilder\Samling\2012\10\image1.jpg
C:\Bilder\Samling\thumbs\2012\10\image1.jpg
```

Hvis originalen er PNG, WEBP eller HEIC/HEIF, får thumbnail-filen likevel
filendelsen `.jpg`.

Kommandoen endrer ikke originalbildene. Hvis en thumbnail allerede finnes og er
nyere enn originalbildet, blir den brukt videre uten å lages på nytt.

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

`--limit` begrenser hvor mange bildefiler som sjekkes:

```powershell
bildebank make-thumbnails --limit 500
```

Dette er nyttig hvis du vil teste kommandoen på en liten del av samlingen først.

## Feil

Hvis én bildefil er korrupt eller ikke kan åpnes, fortsetter kommandoen med
neste bilde. Til slutt viser den hvor mange feil som oppstod.

Hvis det oppstod feil for én eller flere filer, avslutter kommandoen med
exit-code `2`.

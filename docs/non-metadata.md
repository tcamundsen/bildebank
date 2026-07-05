# non-metadata
<!-- CLI-HELP-START -->
```text
usage: bildebank non-metadata [valg]

options:
  -h, --help     show this help message and exit
  --with-source  Vis filen i kilden i tillegg til importert fil
```
<!-- CLI-HELP-END -->

`non-metadata` lister filer der datoen ikke kom fra metadata.

## Referanse

```powershell
bildebank non-metadata
bildebank non-metadata --with-source
```

## Hva kommandoen gjør

Bildebank prøver helst å bruke dato fra metadata. Hvis det ikke finnes, kan den
bruke dato fra filnavn, filens endringstidspunkt eller ukjent dato. Kommandoen
`non-metadata` lar deg se hvilke filer dette gjelder.

Mulighet for å rydde bilder som har dette problemet kommer i en seinere utgave av
programmet.

`--with-source` viser også filen i kilden.

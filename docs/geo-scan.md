# geo-scan
<!-- CLI-HELP-START -->
```text
usage: bildebank geo-scan [valg]

options:
  -h, --help            show this help message and exit
  --force               Les GPS-metadata på nytt for filer som allerede er
                        scannet
  --only-missing        Scan bare filer uten GPS-data og uten tidligere GPS-
                        resultat. Dette er standard.
  --retry-missing       Prøv også filer som tidligere ble scannet uten GPS
                        eller med feil
  --override-manual-h3  Ta med filer med manuell H3-lokasjon. Den manuelle
                        H3-lokasjonen overskrives av GPS fra metadata, eller
                        slettes hvis filen ikke har GPS.
  --limit LIMIT         Maks antall filer som skal scannes
  --verbose             Vis filer uten GPS eller med feil
  --exiftool EXIFTOOL   Path til exiftool. Standard er Bildebanks managed
                        ExifTool, ellers exiftool fra PATH.
  --batch-size BATCH_SIZE
                        Antall filer per ExifTool-kall. Standard: 200
```
<!-- CLI-HELP-END -->

`geo-scan` leser GPS-koordinater fra metadata i importerte bilder og lagrer
resultatet i Bildebank-databasen.

Kommandoen endrer ikke bildefilene.

```powershell
bildebank geo-scan
bildebank geo-scan --retry-missing
bildebank geo-scan --limit 100
bildebank geo-scan --force
bildebank geo-scan --override-manual-h3
```

`geo-scan` bruker ExifTool til å lese metadata. Vanlig Windows-installasjon
legger ExifTool i programmappen til Bildebank:

```text
C:\Users\Tom\kode\bildebank\bildebank-tools\exiftool
```

Hvis ExifTool mangler, kjør:

```powershell
bildebank exiftool-install
```

Du kan også angi plassering selv:

```powershell
bildebank geo-scan --exiftool "C:\Tools\exiftool.exe"
```

Vanlige valg:

- `--limit N` scanner maks N bilder.
- `bildebank geo-scan` scanner bare bilder som mangler GPS-data og ikke har
  tidligere GPS-resultat.
- `--retry-missing` prøver også bilder som tidligere ble scannet uten GPS eller
  med feil.
- `--force` leser GPS-metadata på nytt også for bilder som allerede er scannet.
- `--only-missing` betyr det samme som standardoppførselen og finnes for
  kompatibilitet.
- `--override-manual-h3` scanner også bilder der sted er satt manuelt med
  H3-celle. Hvis metadata inneholder GPS, erstatter GPS-lokasjonen den manuelle
  H3-lokasjonen. Hvis metadata ikke inneholder GPS, fjernes den manuelle
  H3-lokasjonen.
- `--verbose` viser mer informasjon om filer uten GPS eller med feil.

Slettede bilder, altså bilder som er flyttet til `deleted/`, scannes ikke.

Hvis ExifTool ikke finner GPS i en fil, registreres det som "uten GPS". Hvis
ExifTool ikke klarer å scanne en fil, lagrer Bildebank bare en kort feilmarkør
i databasen. Selve ExifTool-feilmeldingen lagres ikke.

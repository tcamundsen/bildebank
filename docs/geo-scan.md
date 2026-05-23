# geo-scan
<!-- CLI-HELP-START -->
```text
usage: bildebank geo-scan [valg]

options:
  -h, --help            show this help message and exit
  --force               Les GPS-metadata på nytt for filer som allerede er
                        scannet
  --only-missing        Scan bare filer uten GPS-data og uten tidligere GPS-
                        resultat
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
bildebank geo-scan --limit 100
bildebank geo-scan --force
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
- `--force` leser GPS-metadata på nytt også for bilder som allerede er scannet.
- `--only-missing` scanner bare bilder uten GPS-data og uten tidligere
  GPS-resultat.
- `--verbose` viser mer informasjon om filer uten GPS eller med feil.

Slettede bilder, altså bilder som er flyttet til `deleted/`, scannes ikke.

Hvis ExifTool ikke finner GPS i en fil, registreres det som "uten GPS". Hvis
ExifTool ikke klarer å scanne en fil, lagrer Bildebank bare en kort feilmarkør
i databasen. Selve ExifTool-feilmeldingen lagres ikke.

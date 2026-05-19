# geo-scan

`geo-scan` leser GPS-koordinater fra metadata i importerte bilder og lagrer
resultatet i Bildebank-databasen.

Kommandoen endrer ikke bildefilene.

```powershell
bildebank geo-scan
bildebank geo-scan --limit 100
bildebank geo-scan --force
```

`geo-scan` bruker ExifTool til å lese metadata. På Windows kan ExifTool enten
ligge i bildesamlingen som `exiftool.exe`, eller finnes i `PATH`.

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

# date-set

```text
usage: bildebank date-set [valg] fil (--date DATO | --between FRA TIL)

Sett manuell dato i Bildebank uten å endre originalfilen.

options:
  -h, --help            show this help message and exit
  --date DATE           Eksakt eller omtrentlig midtdato, YYYY-MM-DD
  --between FRA TIL     Usikkert datointervall, YYYY-MM-DD YYYY-MM-DD
  --uncertainty UNCERTAINTY
                        Usikkerhet rundt --date, for eksempel 3d, 2w, 1m eller
                        1y
  --note NOTE           Fritekstnotat om hvorfor datoen er satt
```

`date-set` setter dato i Bildebank-databasen uten å endre bildefilen.

Eksakt dato:

```powershell
bildebank date-set "2026\01\IMG_1234.jpg" --date 2004-07-18
```

Omtrentlig dato:

```powershell
bildebank date-set "2026\01\IMG_1234.jpg" --date 2004-07-15 --uncertainty 1m
```

Intervall:

```powershell
bildebank date-set "2026\01\IMG_1234.jpg" --between 2004-06-01 2004-08-31
```

Du kan også legge ved notat:

```powershell
bildebank date-set "2026\01\IMG_1234.jpg" --date 2004-07-15 --uncertainty 1m --note "Kamera hadde feil årstall"
```

Filen blir liggende der den er, men bildebrowseren bruker manuell dato for
sortering og månedsvisning.

# exiftool-install

<!-- CLI-HELP-START -->
```text
usage: bildebank exiftool-install [valg]

Last ned og installer ExifTool i programmappen.

options:
  -h, --help  show this help message and exit
  --force     Installer ExifTool på nytt selv om den allerede finnes.
```
<!-- CLI-HELP-END -->

`exiftool-install` laster ned ExifTool for Windows og legger den i
programmappen til Bildebank:

```text
C:\Users\Tom\kode\bildebank\bildebank-tools\exiftool
```

Kommandoen støttes bare på Windows. På Linux installeres ExifTool med
pakkesystemet, for eksempel `sudo apt install libimage-exiftool-perl`.

Kjør kommandoen hvis `geo-scan` sier at ExifTool mangler:

```powershell
bildebank exiftool-install
```

Hvis installasjonen ser ødelagt ut, kan du installere på nytt:

```powershell
bildebank exiftool-install --force
```

ExifTool skal ikke ligge i bildesamlingen. Bildesamlingen skal bare inneholde
bildene, Bildebank-databasen og filer Bildebank lager for samlingen.

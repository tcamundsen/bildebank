# exiftool-metadata-gaps

<!-- CLI-HELP-START -->
```text
usage: bildebank exiftool-metadata-gaps [valg]

options:
  -h, --help            show this help message and exit
  --exiftool EXIFTOOL   Path til exiftool.exe. Standard er Bildebanks managed
                        ExifTool, ellers exiftool fra PATH.
  --batch-size BATCH_SIZE
                        Antall filer per ExifTool-kall. Standard: 200
```
<!-- CLI-HELP-END -->

`exiftool-metadata-gaps` finner metadata-datoer som ExifTool ser, men som
Bildebank ikke leser ennå.

Dette er en feilsøkingskommando for å forbedre metadata-lesingen i
Bildebank. Ikke ment for vanlige brukere.

Kommandoen bruker ExifTool fra Bildebanks programmappe. Hvis ExifTool mangler,
kjør:

```powershell
bildebank exiftool-install
```

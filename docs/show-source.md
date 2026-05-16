# show-source
<!-- CLI-HELP-START -->
```text
usage: bildebank show-source [valg] fil

positional arguments:
  fil         Importert målfil

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`show-source` viser hvilke kilder en importert fil kom fra.

Eksempel:

```powershell
bildebank show-source "2024\07\IMG_1234.jpg"
```
Hvis samme fil finnes i flere kilder, viser kommandoen flere kildehenvisninger.


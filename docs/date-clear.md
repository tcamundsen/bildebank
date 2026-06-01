# date-clear

```text
usage: bildebank date-clear [valg] fil

Fjern manuell dato fra Bildebank uten å endre originalfilen.

options:
  -h, --help  show this help message and exit
```

`date-clear` fjerner manuell dato fra en importert fil. Etterpå bruker
Bildebank igjen datoen som ble funnet ved import eller metadataoppfrisking.

```powershell
bildebank date-clear "2026\01\IMG_1234.jpg"
```

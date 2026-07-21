# ffmpeg-install

<!-- CLI-HELP-START -->
```text
usage: bildebank ffmpeg-install [valg]

Last ned og installer FFmpeg og FFprobe i programmappen.

options:
  -h, --help  show this help message and exit
  --force     Installer FFmpeg på nytt selv om riktig versjon allerede finnes.
```
<!-- CLI-HELP-END -->

Denne Windows-kommandoen laster ned FFmpeg og FFprobe som Bildebank trenger for
å lage MP4-avspillingskopier av AVI-videoer. Programmene legges i
`bildebank-tools\ffmpeg` under Bildebank-programmappen. Du trenger ikke legge
dem i Windows PATH.

Vanligvis trenger du ikke kjøre kommandoen selv. Nye installasjoner, vanlige
oppdateringer og Bildebank-vinduet kontrollerer installasjonen automatisk.
Hvis nedlastingen feiler, fullføres programoppdateringen likevel og Bildebank
prøver igjen ved neste oppstart.

Manuell reparasjon:

```powershell
bildebank ffmpeg-install --force
```

Bildebank kontrollerer SHA-256 for nedlastingen før den pakkes ut, og tester
både FFmpeg, FFprobe og H.264-støtten før den nye installasjonen tas i bruk.


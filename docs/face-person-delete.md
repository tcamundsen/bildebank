# face-person-delete
<!-- CLI-HELP-START -->
```text
usage: bildebank face-person-delete [valg] navn

Slett person fra ansiktsdatabasen

positional arguments:
  navn        Personnavn

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`face-person-delete` sletter en person fra ansiktsdatabasen.

Eksempel:

```powershell
bildebank face-person-delete "Kari"
```

Kommandoen krever bekreftelse. Den sletter personen, bekreftede
ansiktskoblinger, manuelle person-i-bilde-koblinger og forslag for personen.
Den sletter ingen bilder og ingen scannede ansikter.

Det samme kan gjøres i nettleseren når `bildebank run-server` kjører: åpne
siden **Personer**, og trykk `slett person` bak navnet.

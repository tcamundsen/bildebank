# update

<!-- CLI-HELP-START -->
```text
usage: bildebank update [valg]

Oppdater Bildebank til siste versjon fra GitHub.

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`update` oppdaterer Bildebank-programmet til siste versjon fra GitHub
og laster ned eventuelle nye biblioteker som brukes.

Oppdateringen kontrollerer også den lokale FFmpeg-installasjonen. Dermed får
eksisterende brukere støtten som trengs for AVI-avspillingskopier, ikke bare
nye installasjoner. Hvis FFmpeg-nedlastingen feiler, beholdes den fullførte
Bildebank-oppdateringen. Programmet viser en advarsel og prøver igjen ved neste
oppstart.

Eksempel:

```powershell
bildebank update
```

Du kan også starte samme oppdatering fra Bildebank-vinduet med knappen
`Oppdater Bildebank`.

Etter en oppgradering kan det hende programmet ber deg kjøre
`bildebank migrate` for å oppdatere databasen.

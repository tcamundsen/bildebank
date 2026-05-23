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

Eksempel:

```powershell
bildebank update
```

Etter en oppgradering kan det hende programmet ber deg kjøre
`bildebank migrate` for å oppdatere databasen.

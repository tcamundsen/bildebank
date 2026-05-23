# face-person-rename

<!-- CLI-HELP-START -->
```text
usage: bildebank face-person-rename [valg] gammelt_navn nytt_navn

Endre navn på person i ansiktsdatabasen

positional arguments:
  gammelt_navn  Eksisterende personnavn
  nytt_navn     Nytt personnavn

options:
  -h, --help    show this help message and exit
```
<!-- CLI-HELP-END -->


`face-person-rename` endrer navn på en person i ansiktsdatabasen.

Kan også gjøres i nettleser: start `bildebank run-server`, åpne **Personer**,
og trykk **endre navn** bak personnavnet.

## Referanse

```powershell
bildebank face-person-rename "Krai" "Kari"
```

Kommandoen endrer bare personnavnet. Bekreftede ansiktskoblinger og forslag
beholdes.

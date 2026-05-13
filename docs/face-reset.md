# face-reset

`face-reset` sletter ansiktsdata.

Kommandoen krever alltid bekreftelse.

## Referanse

```powershell
bildebank face-reset
bildebank face-reset --all
bildebank face-reset --keep-scan
```

Uten nivåvalg er standard det samme som `--keep-scan`.
Det er dette som er befalt hvis du vil starte på nytt med å
finne personer, for da slipper du å kjøre `face-scan` på nytt.

`--keep-scan` beholder resultatene fra `face-scan`, men sletter personer,
bekreftelser og forslag.

`--all` sletter hele face-databasen. Bruk dette bare hvis du også vil slette
resultatene fra `face-scan`.

Kommandoen sletter ingen bilder og endrer ikke den vanlige Bildebank-databasen.

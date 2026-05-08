# face-reset

`face-reset` sletter eksperimentelle ansiktsdata.

Kommandoen krever alltid bekreftelse.

## Referanse

```powershell
bildebank face-reset --all
bildebank face-reset --keep-scan
bildebank face-reset --keep-scan-and-groups
```

`--all` sletter hele face-databasen.

`--keep-scan` beholder resultatene fra `face-scan`, men sletter grupper,
personer, bekreftelser og forslag.

`--keep-scan-and-groups` beholder resultatene fra `face-scan` og `face-group`,
men sletter personer, bekreftelser og forslag.

Kommandoen sletter ingen bilder og endrer ikke den vanlige Bildebank-databasen.


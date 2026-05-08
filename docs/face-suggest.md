# face-suggest

`face-suggest` foreslår personer for ukjente ansikter.

## Referanse

```powershell
bildebank face-suggest
bildebank face-suggest --threshold 0.70
```

Forslagene bygger på ansikter du allerede har bekreftet med
`face-person-add-face` eller `face-person-add-group`.

Høyere `--threshold` gir strengere forslag. Forslagene er ikke bekreftede før
du selv kobler ansiktet til personen.


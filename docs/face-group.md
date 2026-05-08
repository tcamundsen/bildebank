# face-group

`face-group` lager mulige grupper av ansikter som ligner hverandre.

## Referanse

```powershell
bildebank face-group
bildebank face-group --threshold 0.65
```

Gruppene er forslag, ikke bekreftede personer.

Høyere `--threshold` betyr strengere likhet. Det gir vanligvis færre og mindre
grupper, men mindre risiko for feilblanding. Lavere verdi gir vanligvis flere
og større grupper, men større risiko for at ulike personer blandes.

Det er trygt å kjøre `face-group` på nytt. Bekreftede personer lagres på
ansikt-id, ikke gruppe-id. Lag `face-groups.html` på nytt etterpå:

```powershell
bildebank make-face-groups-browser
```

Se også [`insightface`](insightface.md).


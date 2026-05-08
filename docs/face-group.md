# face-group

`face-group` lager mulige grupper av ansikter som ligner hverandre.

## Referanse

```powershell
bildebank face-group
bildebank face-group --threshold 0.65
bildebank face-group --max-size 200
```

Gruppene er forslag, ikke bekreftede personer.

Høyere `--threshold` betyr strengere likhet. Det gir vanligvis færre og mindre
grupper, men mindre risiko for feilblanding. Lavere verdi gir vanligvis flere
og større grupper, men større risiko for at ulike personer blandes.

`--max-size` kan brukes for å hoppe over grupper som er for store til å
kontrollere manuelt:

```powershell
bildebank face-group --max-size 200
```

Da skrives bare grupper med 200 ansikter eller færre til `face-groups.html`.
Store grupper slettes ikke fra scanningen; de blir bare ikke med i denne
grupperingen. Hvis du får store grupper, kan du også prøve høyere `--threshold`.

Det er trygt å kjøre `face-group` på nytt. Bekreftede personer lagres på
ansikt-id, ikke gruppe-id. Lag `face-groups.html` på nytt etterpå:

```powershell
bildebank make-face-groups-browser
```

Se også [`insightface`](insightface.md).

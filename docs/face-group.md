# face-group

`face-group` lager mulige grupper av ansikter som ligner hverandre.

## Referanse

```powershell
bildebank face-group
bildebank face-group --threshold 0.65
bildebank face-group --max-size 200
bildebank face-group --max-size 0
bildebank face-group -o "mine-grupper.html"
```

`face-group` beregner gruppene og lager `face-groups.html` i samme kjøring.
Gruppene er forslag, ikke bekreftede personer.

Høyere `--threshold` betyr strengere likhet. Det gir vanligvis færre og mindre
grupper, men mindre risiko for feilblanding. Lavere verdi gir vanligvis flere
og større grupper, men større risiko for at ulike personer blandes.

Som standard skriver `face-group` bare grupper med 50 ansikter eller færre.
Dette gjør `face-groups.html` raskere å åpne, og gjør det mer realistisk å
kontrollere gruppen før den kobles til en person.

`--max-size` kan brukes hvis du vil velge en annen grense:

```powershell
bildebank face-group --max-size 200
```

Da skrives bare grupper med 200 ansikter eller færre. Store grupper slettes
ikke fra scanningen; de blir bare ikke med i denne grupperingen.

Hvis du vil skru av maksgrensen helt, bruker du:

```powershell
bildebank face-group --max-size 0
```

Hvis du får store grupper, kan du også prøve høyere `--threshold`.

Det er trygt å kjøre `face-group` på nytt. Bekreftede personer lagres på
ansikt-id, ikke gruppe-id. `face-groups.html` lages på nytt hver gang, slik at
gruppe-id-ene på siden hører sammen med den siste grupperingen.

Hvis du vil skrive HTML-filen et annet sted, bruker du `-o`.

Hvis et bilde har flere ansikter, viser `face-groups.html` lenker til andre
grupper som har ansikter fra samme bilde. Det gjør det lettere å kontrollere
hvilke andre personer som finnes i bildet.

Se også [`insightface`](insightface.md).

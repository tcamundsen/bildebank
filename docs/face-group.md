# face-group

`face-group` lager mulige grupper av ansikter som ligner hverandre.

## Referanse

```powershell
bildebank face-group
bildebank face-group --threshold 0.65
bildebank face-group --max-size 200
bildebank face-group --max-size 0
bildebank face-group --include-known
bildebank face-group -o "mine-grupper.html"
```

`face-group` beregner gruppene og lager `face-groups.html` i samme kjøring.
Gruppene er forslag, ikke bekreftede personer.

Høyere `--threshold` betyr strengere likhet. Det gir vanligvis færre og mindre
grupper, men mindre risiko for feilblanding. Lavere verdi gir vanligvis flere
og større grupper, men større risiko for at ulike personer blandes.

Som standard viser `face-group` høyst 50 ansikter fra hver gruppe. Hvis en
gruppe egentlig har flere ansikter, blir den forkortet i `face-groups.html`.
Dette gjør siden raskere å åpne, og gjør det mer realistisk å kontrollere
gruppen før den kobles til en person.

`--max-size` kan brukes hvis du vil velge en annen grense:

```powershell
bildebank face-group --max-size 200
```

Da vises høyst 200 ansikter fra hver gruppe.

Viktig: `face-person-add-group` legger bare til ansiktene som vises i
`face-groups.html`. Hvis en stor gruppe er forkortet til 50 ansikter, er det
bare de 50 synlige ansiktene som blir koblet til personen.

Store grupper slettes ikke fra scanningen. Ansiktene som ikke vises kan senere
komme tilbake som forslag med `face-suggest`, eller vises hvis du kjører
`face-group` på nytt med høyere `--max-size`.

Hvis du vil skru av maksgrensen helt, bruker du:

```powershell
bildebank face-group --max-size 0
```

Hvis du får store grupper, kan du også prøve høyere `--threshold`.

Det er trygt å kjøre `face-group` på nytt. Bekreftede personer lagres på
ansikt-id, ikke gruppe-id. `face-groups.html` lages på nytt hver gang, slik at
gruppe-id-ene på siden hører sammen med den siste grupperingen.

Som standard skjules grupper der alle synlige ansikter allerede er bekreftet
som samme person. Hvis du vil se disse ferdige gruppene likevel, bruker du:

```powershell
bildebank face-group --include-known
```

Hvis du vil skrive HTML-filen et annet sted, bruker du `-o`.

Hvis et bilde har flere ansikter, viser `face-groups.html` en knapp for alle
ansikter i bildet. Den åpner en detaljvisning der samme bilde vises én gang per
ansikt, med rektangel og `face-id`. Det gjør det lettere å finne riktig
`face-id` hvis du vil legge til ett enkelt ansikt med `face-person-add-face`.

Hvis andre ansikter i bildet også finnes i synlige grupper, viser siden lenker
til disse gruppene.

Se også [`insightface`](insightface.md).

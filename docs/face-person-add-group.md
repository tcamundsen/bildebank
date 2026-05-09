# face-person-add-group

`face-person-add-group` kobler en ansiktsgruppe til en person.

## Referanse

```powershell
bildebank face-person-add-group "Kari" 12
```

Tallet er gruppe-id fra `face-groups.html`. Denne kommandoen må bare
kjøres på grupper der programmet har markert samme person i alle bildene.

Personen må være opprettet først med `face-person-create`. Kommandoen lagrer
koblingen på hvert enkelt ansikt i gruppen, ikke på gruppe-id.


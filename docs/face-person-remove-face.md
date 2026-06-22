# face-person-remove-face

`face-person-remove-face` fjerner ett ansikt fra en person.

## Referanse

```powershell
bildebank face-person-remove-face "Kari" 798
```

Tallet `798` er `face-id`.

Du finner `face-id` i personsidene, `face-suggest` eller vanlig `index.html`
med knappen `Ansikter i bildet`.

```text
face-id 798, deteksjon 0.931
```

Kommandoen fjerner bare koblingen mellom personen og ansiktet. Den sletter ikke
bildet og sletter ikke selve ansiktet fra face-databasen.

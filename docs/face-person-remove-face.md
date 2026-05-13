# face-person-remove-face

`face-person-remove-face` fjerner ett ansikt fra en person.

## Referanse

```powershell
bildebank face-person-remove-face "Kari" 798
```

Tallet `798` er `face-id`.

Du finner `face-id` i `faces.html`, under bildet:

```text
face-id 798, deteksjon 0.931
```

Du kan også finne `face-id` i personsidene. `faces.html` er først og fremst
for debugging og kan bli stor. Lag den helst med en grense:

```powershell
bildebank make-face-browser --limit 100
```

Kommandoen fjerner bare koblingen mellom personen og ansiktet. Den sletter ikke
bildet og sletter ikke selve ansiktet fra face-databasen.

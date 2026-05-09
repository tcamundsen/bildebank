# face-person-remove-face

`face-person-remove-face` fjerner ett ansikt fra en person.

## Referanse

```powershell
bildebank face-person-remove-face "Kari" 798
```

Tallet `798` er `face-id`, ikke gruppenummer.

Du finner `face-id` i `face-groups.html`, under bildet:

```text
face-id 798, gruppelikhet 0.842, deteksjon 0.931
```

Du kan også finne `face-id` i `faces.html`, som lages med:

```powershell
bildebank make-face-browser
```

Kommandoen fjerner bare koblingen mellom personen og ansiktet. Den sletter ikke
bildet og sletter ikke selve ansiktet fra face-databasen.

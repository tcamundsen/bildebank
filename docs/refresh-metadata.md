# refresh-metadata

`refresh-metadata` sjekker filer uten metadata på nytt.

## Referanse

```powershell
bildebank refresh-metadata --dry-run
bildebank refresh-metadata
bildebank refresh-metadata --verbose
```

## Hva kommandoen gjør

Hvis Bildebank senere klarer å lese metadata som manglet ved import, kan filen
flyttes til riktig datomappe.

Start med `--dry-run` for å se hva som ville skjedd uten å endre filer eller
database.


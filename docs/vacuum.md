# vacuum

`vacuum` pakker Bildebank-databasen slik at SQLite-filen kan krympe fysisk.

Kommandoen endrer ikke bildefilene.

```powershell
bildebank vacuum
```

Dette kan være nyttig etter migreringer eller andre databaseendringer som
frigjør mye plass inne i databasen. SQLite bruker ellers ofte den frigjorte
plassen på nytt senere uten å gjøre selve databasefilen mindre.

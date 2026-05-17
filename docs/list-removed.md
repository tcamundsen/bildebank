# list-removed

`list-removed` viser filer som er flyttet til `deleted/`.

## Referanse

```powershell
bildebank list-removed
```

## Hva kommandoen gjør

Når du bruker `remove`, flyttes filen til `deleted/` og markeres som slettet i
databasen. `list-removed` viser disse filene.

Filer i listen kan flyttes tilbake med `undelete`:

```powershell
bildebank undelete "deleted\2024\01\IMG_0001.jpg"
```

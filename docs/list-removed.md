# list-removed
<!-- CLI-HELP-START -->
```text
usage: bildebank list-removed [valg]

List filener som er slettet med `remove`

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`list-removed` viser filer som er flyttet til `deleted/` med kommandoen
[`remove`](remove.md).

Du kan også se fra nettleseren ved å klikke **Innstillinger** og  **Slettede
bilder**.

Når du bruker `remove`, flyttes filen til `deleted/` og markeres som slettet i
databasen. `list-removed` viser disse filene.

Filer i listen kan flyttes tilbake med `undelete`:

```powershell
bildebank undelete "deleted\2024\01\IMG_0001.jpg"
```

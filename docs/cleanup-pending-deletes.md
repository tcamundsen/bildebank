# cleanup-pending-deletes

<!-- CLI-HELP-START -->
```text
usage: bildebank cleanup-pending-deletes [valg]

Vis pending-delete-køen, eller prøv eksplisitt å slette filer som ikke lenger
har database-referanser.

options:
  -h, --help     show this help message and exit
  --list         Vis pending filer og siste feilmelding. Dette er standard.
  --apply        Prøv å slette pending filer.
  --limit LIMIT  Maks antall pending filer som forsøkes med --apply.
```
<!-- CLI-HELP-END -->

Kommandoen viser som standard filer som venter på fysisk sletting:

```powershell
bildebank cleanup-pending-deletes
```

Det samme kan skrives eksplisitt:

```powershell
bildebank cleanup-pending-deletes --list
```

Ingen filer slettes uten `--apply`:

```powershell
bildebank cleanup-pending-deletes --apply
```

Før hver sletting kontrollerer Bildebank stien på nytt og sjekker at filen
ikke lenger finnes i importdatabasen. Feil på én fil stopper ikke kontrollen
av de neste filene.

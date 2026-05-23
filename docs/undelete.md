# undelete
<!-- CLI-HELP-START -->
```text
usage: bildebank undelete [valg] fil

Gjenopprett et bilde du har slettet med `remove`

positional arguments:
  fil         Slettet fil under deleted/

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`undelete` gjenoppretter et bilde eller en video du har slettet med
[`remove`](remove.md).

Dette kan du også gjøre fra nettleseren ved å trykk **Innstillinger**
og deretter **Slettede bilder**.

Eksempel:

```powershell
bildebank undelete "deleted\2024\01\IMG_0001.jpg"
```

`fil` må være stien til filen slik den ligger under `deleted`-mappen. Du kan
ikke peke på den opprinnelige stien, for eksempel `2024\01\IMG_0001.jpg`.

## Hva kommandoen gjør

`undelete` flytter filen tilbake til stien den hadde før [`remove`](remove.md)
ble kjørt, og fjerner slettemarkeringen i databasen.

Hvis målfilen allerede finnes, stopper kommandoen uten å flytte noe.

## Se slettede filer

Du kan finne filer som kan flyttes tilbake med:

```powershell
bildebank list-removed
```

# migrate
<!-- CLI-HELP-START -->
```text
usage: bildebank migrate [valg]

Validerer og oppgraderer databasen etter en programoppdatering.

options:
  -h, --help  show this help message and exit
  --check     Vis hva migreringen vil gjøre uten å endre databasen
```
<!-- CLI-HELP-END -->

`migrate` oppgraderer Bildebank-databasen i en bildesamling til nytt format.

## Når trenger du migrate?

Noen programoppdateringer endrer hvordan Bildebank lagrer informasjon i
databasen. Da kan Bildebank si fra om at databasen må migreres før du kan
fortsette.

Gå til bildesamlingsmappen før du kjører kommandoen:

```powershell
cd "$HOME\BildeSamling"
bildebank migrate --check
bildebank migrate
```

## Hva gjør --check?

`--check` viser om databasen trenger migrering, uten å endre databasen.

Det er trygt å kjøre:

```powershell
bildebank migrate --check
```

## Backup

Når `bildebank migrate` faktisk endrer databasen, lager programmet en backup av
databasen først.

Hvis migreringen feiler, skal databasen ikke oppgraderes, og backupen beholdes.

## Migrering til v7

V7 rydder gamle GPS-feilmeldinger som tidligere kunne bli svært lange. Etter
migreringen lagrer Bildebank bare en kort feilmarkør for filer der GPS-scanning
feilet.

Hvis databasefilen fortsatt er stor etter migreringen, kan du pakke den med
[`vacuum`](vacuum.md):

```powershell
bildebank vacuum
```

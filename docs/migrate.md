# migrate

`migrate` oppgraderer Bildebank-databasen i en bildesamling til nytt format.

## Referanse

```powershell
bildebank migrate [valg]
```

Vanlig kontroll først:

```powershell
bildebank migrate --check
```

Kjør migrering:

```powershell
bildebank migrate
```

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

## Migrering til v4

Migrering til databaseformat v4 gjelder bare brukere som har opprettet
bildesamlingsdatabasen med en eldre versjon av Bildebank.

Nye bildesamlinger som er opprettet med en nyere versjon av Bildebank bruker
nytt databaseformat allerede, og trenger ikke denne migreringen.

I v4 får alle kilder et navn. Gamle kilder som mangler navn får navn fra
mappenavnet, for eksempel `sommer2024`. Hvis flere kilder ville fått samme
navn, legger Bildebank til `-1`, `-2` og så videre.

V4 fjerner også gamle interne kilde-typer fra databasen. For brukeren betyr det
at alle importer behandles likt:

```powershell
bildebank import --name "Navn" "path\til\kilde"
```

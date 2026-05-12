# Database v4 migration

Dette dokumentet beskriver den historiske oppryddingen til databaseformat v4.
`app-design.md` skal beskrive dagens situasjon, mens denne filen beholder
overgangsreglene for vedlikehold og feilsøking.

## Bakgrunn

Før v4 hadde databasen en enklere proveniensmodell:

- `duplicate_findings` lagret duplikater som egne rader.
- `files.source_id`, `files.source_path` og `files.source_path_key` pekte på
  én hovedkilde for målfilen.
- `sources.kind` og `file_sources.kind` skilte mellom kilde- og importtype.
- `errors` kunne ha en foreign key til `sources`, som gjorde det vanskelig å
  slette eller rydde opp i kilder.

I v4 er dette normalisert:

- `files` er sannheten om målfilene i samlingen.
- `file_sources` er sannheten om kildefilforekomster.
- `sources` har `name NOT NULL UNIQUE`.
- `errors` kan lagre source-historikk uten å blokkere opprydding.

## Migrering til v4

Migreringen skulle:

- ta backup før endring
- ta målmappelås
- kjøre i én transaksjon
- bygge `file_sources` fra `files` og `duplicate_findings`
- bygge om `files` uten `source_id`, `source_path` og `source_path_key`
- bygge om `errors` uten gammel foreign key til `sources`
- gi navn til gamle navnløse kilder
- bygge om `sources` uten `kind`
- bygge om `file_sources` uten `kind`
- fjerne `duplicate_findings`
- committe først etter at `PRAGMA foreign_key_check` og `PRAGMA integrity_check`
  var OK

Hvis en kontroll feilet, skulle migreringen rulles tilbake og backupen
beholdes.

## Praktisk status

Denne migreringen er allerede implementert og testet i kodebasen. Nye
databaser opprettes direkte med v4-skjema, og `bildebank migrate` brukes bare
når en eldre bildesamling fortsatt trenger oppgradering.

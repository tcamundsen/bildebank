# Database-moduler

Dette dokumentet beskriver hvordan databasekoden er delt opp. Målet er å gjøre
videre endringer tryggere ved at hvert domene har et tydelig eierskap.

## Offentlig API

`bildebank/db.py` er fortsatt offentlig facade for resten av programmet.
Eksisterende kode bør kunne importere fra `bildebank.db` uten å vite hvilken
intern modul som eier implementasjonen.

Ny kode kan importere direkte fra en intern modul når det gjør eierskapet
tydeligere, men ikke bruk dette som grunn til å spre ansvar på tvers av domener.
Hvis en funksjon naturlig hører til et domene under, legg den i den modulen og
re-eksporter den fra `db.py` hvis gammel kode eller bredere API trenger navnet.

## Modulansvar

### `db_core.py`

Eier felles database- og path-hjelpere:

- databasefilnavn og target-path-oppslag
- `connect`
- meta-tabellhjelpere
- path-normalisering og relative target-paths
- små generelle SQL-hjelpere som `table_exists`, `table_columns` og
  `ensure_column`

Denne modulen skal ikke eie domenespesifikk SQL for filer, kilder, tags, geo
eller fil-livssyklus.

### `db_schema.py`

Eier schema, migrering, validering og repair:

- `SCHEMA_VERSION`
- schemaopprettelse
- migreringsplaner og migrering
- schema-/helsevalidering
- interne repairs av eksisterende database
- schema-konstanter som brukes av flere domener, for eksempel H3-kolonner og
  browser-datosortering

Runtime-operasjoner bør normalt ikke legges hit. Unntaket er kode som er
spesifikt knyttet til migrering eller repair av gamle schema.

### `db_sources.py`

Eier kilder:

- `Source`
- oppslag på kilder
- opprettelse av navngitte kilder
- source-status, inkludert imported/error/pending

Modulen skal ikke eie koblingen mellom kilder og filer. Den koblingen går via
`file_sources` og hører til `db_files.py`.

### `db_files.py`

Eier filrader og proveniens mellom filer og kilder:

- `files`
- `file_sources`
- importinnsetting
- duplikatoppslag
- integritetsrader
- unimport-planer som avgjør om en fil kan fjernes eller bare mister proveniens
- browser-/listeoppslag for filer som ikke naturlig hører til geo, tags eller
  lifecycle
- normalisering og lagring av den ene valgfrie kommentaren på `files`

Viktig grense: `files` og `file_sources` skal behandles som ett domene her. Ikke
splitt dem i separate moduler bare fordi de er separate tabeller. Import,
rescan, unimport og duplikathåndtering er avhengige av at denne koblingen
forstås samlet.

### `db_tags.py`

Eier tags:

- tag-konstanter og systemtagg-navn
- tag-normalisering og `name_key`
- oppretting, endring og sletting av brukertagger
- tagging/untagging av filer
- tag-lister og taggede filer

Schemaopprettelse og repair av tag-tabellene ligger fortsatt i `db_schema.py`,
men bruker normaliseringsreglene fra `db_tags.py`.

### `db_geo.py`

Eier runtime-operasjoner for GPS/H3 og steder:

- filer som skal geo-scannes
- oppdatering av GPS/H3-felter på `files`
- manuell H3-lokasjon
- geo-statistikk
- H3-områdeoppslag og filer i områder
- navn på H3-celler
- egne geo-steder og deres H3-celler

Schema, migrering og H3-kolonnedefinisjoner ligger i `db_schema.py`.

### `db_lifecycle.py`

Eier databaseoppdateringer for fil-livssyklus:

- manuell dato på filer
- pending file moves
- markering av filer som deleted/undeleted
- oppdatering av filplassering etter flytting/import

Denne modulen skriver database-radene som støtter trygg filflytting, men den
skal ikke selv flytte filer på disk. Faktisk filflytting, låsing, verifisering
og rollback-håndtering hører til høyere nivåer som `file_lifecycle.py`,
`file_moves.py` og importerflyten.

## Endringsregler

- Hold `db.py` kompatibel som facade når navn flyttes.
- Flytt kode uten atferdsendring først; gjør funksjonelle endringer i egne
  steg.
- Ved endringer i `files`/`file_sources`, test import, duplikat, unimport og
  integritet.
- Ved endringer i lifecycle, test pending moves, remove, undelete og manuell
  dato.
- Ved endringer i schema/migrering, test både ny database og migrering av
  kopier av gamle testdatabaser.
- Ikke gjør filsystemoperasjoner fra lavnivå database-moduler.

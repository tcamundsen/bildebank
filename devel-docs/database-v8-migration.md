# Database v8: H3-opploesning 10 og 11

Status: `schema_version=8` legger til finere H3-kolonner i `files`.

## Bakgrunn

Manuell plassering av bilder trenger finere operativ stedscelle enn
`h3_res9`. V8 utvider databasen med:

- `h3_res10`
- `h3_res11`

## Regler etter v8

- Ny database skal ha H3-kolonner fra `h3_res0` til `h3_res11`.
- `geo-scan` skal fylle alle H3-kolonnene fra GPS-koordinater.
- Manuell H3-lokalisering skal fylle alle H3-kolonnene fra valgt celles
  senterpunkt.
- `bildebank migrate` legger til de nye kolonnene og indeksene.
- Under migrering fylles `h3_res10` og `h3_res11` for eksisterende filer som
  allerede har `gps_lat` og `gps_lon`.
- Migreringen skal ikke skrive GPS-data eller andre metadata tilbake til
  bildefiler.

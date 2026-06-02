# Database v10: kamera i files

Status: `schema_version=10` legger kameradata på `files`.

## Endring

`files` får to nye nullable kolonner:

- `camera_make TEXT`
- `camera_model TEXT`

Import fyller kolonnene fra JPEG EXIF når kamera kan leses. `refresh-metadata`
oppdaterer også kamerakolonnene når en fil sjekkes på nytt.

## Migrering

Migreringen legger bare til kolonnene. Den leser ikke alle eksisterende
bildefiler på nytt, fordi `bildebank migrate` skal være en ren og forutsigbar
databaseendring.

Eksisterende filer får derfor `NULL` i kamerakolonnene frem til metadata
oppdateres eksplisitt.

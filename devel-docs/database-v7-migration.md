# Database v7: korte GPS-feilmarkører

Status: `schema_version=7` fjerner lagring av rå ExifTool-feilmeldinger i
`files.gps_error`.

## Bakgrunn

`geo-scan` kunne tidligere lagre hele ExifTool-feilmeldinger per fil. Ved
batchfeil kunne samme lange stderr-tekst bli lagret på mange filer og gjøre
SQLite-databasen unødvendig stor.

## Regler etter v7

- `geo-scan` skal ikke lagre rå ExifTool-tekst i databasen.
- Filer uten GPS lagres med `gps_lat = NULL`, `gps_lon = NULL` og
  `gps_error = NULL`.
- Per-fil ExifTool-feil lagres som kort markør i `gps_error`.
- Hel batchfeil uten brukbar JSON skal ikke markere filene som ferdig scannet.
- `bildebank migrate` konverterer gamle ikke-NULL GPS-feiltekster til kort
  markør.
- `bildebank vacuum` kan kjøres etter migrering for å krympe SQLite-filen
  fysisk.

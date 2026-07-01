# Database v14-migrering

Status: `schema_version=14` fjerner den gamle `superseded`-kildemodellen.

## Endringer

- `sources.superseded_by_source_id` fjernes.
- `sources.status = 'superseded'` normaliseres til `imported`.
- Kilder, filer og `file_sources` beholdes uendret ellers.

Dette matcher dagens importmodell: overlappende importer representeres som flere
`file_sources`-rader mot samme fil, ikke ved at en kilde erstatter en annen.

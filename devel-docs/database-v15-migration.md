# Database v15-migrering

Status: `schema_version=15` legger til kommentarer på mediefiler.

## Endringer

- `files.comment TEXT` legges til som en nullable kolonne.
- Eksisterende `files`- og `file_sources`-rader beholdes uendret.
- Eksisterende filer får `comment = NULL`.
- Ingen bildefiler flyttes, slettes eller får endret metadata.

Kommentaren ligger på den kanoniske `files`-raden og bevares derfor ved
`remove` og `undelete`. Flere proveniensrader i `file_sources` deler samme
kommentar.

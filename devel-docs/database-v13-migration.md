# Database v13-migrering

Status: `schema_version=13` legger til `files.metadata_datetime`.

Kolonnen lagrer metadata-tidspunkt med sekundpresisjon som tekst på formen
`YYYY-MM-DD HH:MM:SS`. Den brukes foreløpig til konservativ kobling mellom
JPEG og NEF i bildebrowseren.

Migreringen leser ikke bildefiler og backfiller derfor ikke eksisterende rader.
Eksisterende samlinger kan fylle feltet med `bildebank refresh-metadata --rescan`.

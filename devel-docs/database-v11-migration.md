# Database v11: pending filsletting

Status: `schema_version=11` legger til arbeidskøen
`pending_file_deletes`.

Tabellen lagrer target-relative mediestier som databasen har bestemt ikke
lenger skal ha en `files`-referanse, men der fysisk sletting må kunne prøves
igjen etter feil eller avbrudd.

Køen er ikke historikk. Raden fjernes når filen er slettet eller allerede
mangler. Feil beholder raden og oppdaterer `attempts`, `last_error` og
`updated_at`.

Migrering fra v10 oppretter bare tabellen. Ingen eksisterende filer legges
automatisk i køen.

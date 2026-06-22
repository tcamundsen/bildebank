# Database v11: pending filsletting

Status: `schema_version=11` legger til arbeidskøen
`pending_file_deletes`.

Tabellen lagrer target-relative mediestier som databasen har bestemt ikke
lenger skal ha en `files`-referanse, men der fysisk sletting må kunne prøves
igjen etter feil eller avbrudd.

`source_id` lagres som import-/jobb-id uten foreign key, slik at id-en beholdes
selv når importens `sources`-rad er fjernet.

Køen er ikke historikk. Raden fjernes når filen er slettet eller allerede
mangler. Feil beholder raden og oppdaterer `attempts`, `last_error` og
`updated_at`.

Migrering fra v10 oppretter bare tabellen. Ingen eksisterende filer legges
automatisk i køen.

## Unik SHA-256 for aktive filer

`files` har i tillegg en partiell unik indeks:

```sql
CREATE UNIQUE INDEX idx_files_sha256_active_unique
ON files(sha256)
WHERE deleted_at IS NULL;
```

Den eksisterende ikke-unike `idx_files_sha256` beholdes. Før migrering
oppretter den nye indeksen, avvises databasen med beskjed om å kjøre
`bildebank doctor` hvis flere aktive `files`-rader har samme SHA-256.
Slettede rader omfattes ikke av unikheten.

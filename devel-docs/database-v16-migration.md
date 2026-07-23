# Database v16: innholdsidentitet for ventende filsletting

Status: `schema_version=16` legger til innholdsidentitet i
`pending_file_deletes`.

## Endringer

- `expected_sha256 TEXT` legges til.
- `expected_size_bytes INTEGER` legges til.
- Nye køposter fra `unimport` fyller begge kolonnene.
- Eksisterende køposter beholdes med `NULL` i de nye kolonnene.

Før fysisk sletting kontrollerer `cleanup-pending-deletes` at stien ikke har en
ny `files`-referanse, og at filens størrelse og SHA-256 matcher køposten.
Erstattede eller endrede filer slettes ikke.

Legacy-køposter med `NULL` i ett av identitetsfeltene blir stående med en
feilmelding. Migreringen gjetter ikke identitet fra det som tilfeldigvis ligger
på stien under migrering.

Migreringen flytter eller sletter ingen mediefiler.

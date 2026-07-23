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

## Slettede duplikater ved ny import

En `files`-rad som er markert slettet og peker til `deleted/`, deltar fortsatt
i SHA-256-basert duplikatgjenkjenning. Hvis en senere import inneholder samme
filinnhold, skal den nye `file_sources`-raden kobles til den slettede
`files`-raden. Bildet skal ikke aktiveres eller kopieres inn på nytt.

Dette bevarer brukerens sletting på tvers av senere importer. `unimport` av én
av kildene skal beholde den slettede filen så lenge en annen kilde fortsatt
refererer til den. Når siste kilde unimporteres, fjernes `files`-raden og filen
under `deleted/` legges i `pending_file_deletes` for fysisk opprydding.

## Fysisk sletting ved unimport

`unimport` er en annen operasjon enn brukerinitiert `remove`. En forutsetning
for å gjennomføre `unimport` er at alle registrerte originalfiler fortsatt finnes
i kilden og valideres med størrelse og SHA-256 før Bildebank endrer databasen
eller legger filene i `pending_file_deletes`.

Før databaseendringen kontrollerer `unimport` også filene som faktisk vil
miste siste `file_sources`-rad. Hvis en slik fil finnes på disk, men ikke
lenger matcher `files.size_bytes` og `files.sha256`, skal brukeren få en tydelig
advarsel og eksplisitt bekrefte om `unimport` likevel skal fortsette. Dette er
en overstyrbar beskyttelse mot manuelle endringer i bildesamlingen, ikke en ny
schema- eller `pending_file_deletes`-invariant.

Når en fil mister sin siste `file_sources`-rad i `unimport`, skal den derfor
ryddes fysisk via `pending_file_deletes`. Den skal ikke flyttes til karantene
under `deleted/` først. Karantene ville brukt ekstra diskplass og lagt til mer
livssykluskompleksitet uten å være nødvendig for denne flyten, fordi
originalinnholdet allerede er kontrollert i kilden før unimporten får
fortsette.

Ikke foreslå karantene for normal `unimport` uten at kravene endres. Hvis det
senere skal støttes `unimport` uten tilgjengelige originalfiler, må det
vurderes som en separat og eksplisitt tapsrisiko-flyt.

## Senere sikkerhetsendring

Schema v16 beholder denne unimportmodellen, men binder hver ny
`pending_file_deletes`-rad til forventet SHA-256 og størrelse. Se
`devel-docs/database-v16-migration.md`. Eldre køposter uten innholdsidentitet
skal ikke slettes automatisk.

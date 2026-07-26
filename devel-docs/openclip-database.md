# OpenCLIP-database

OpenCLIP-data lagres separat fra hoveddatabasen i:

```text
.bilder-openclip.sqlite3
```

Plasseringen er fast i roten av samlingen. Databasefilen skal være en vanlig
fil uten symlink, hardlink eller Windows reparse point.

Databasen inneholder regenererbare data, men embeddings kan være svært
tidkrevende å beregne på nytt. Eksisterende data skal derfor valideres og
bevares fremfor å repareres eller overskrives automatisk.

## Livssyklus for bilder

Et aktivt bilde beholder OpenCLIP-data så lenge minst én `file_sources`-rad
finnes. `unimport` av én av flere kilder skal ikke rydde disse radene.

`remove` er en eksplisitt beslutning om at bildet ikke lenger skal brukes.
Operasjonen sletter derfor bildets `image_embeddings` og
`image_search_results`, og sletter søkekjøringer som blir tomme.
`undelete` gjenoppretter ikke radene; `image-scan` må bygge embeddings på nytt.

Når `unimport` fjerner siste `file_sources`-rad og dermed `files`-raden, skal
den utføre samme opprydding. Slettingen skjer gjennom en ATTACH-et
OpenCLIP-database i samme transaksjon som hoveddatabaseendringen.

Hoveddatabasens migrering til v17 rydder tilsvarende rester etter eldre
versjoner. Den beholder data for aktive `files`-rader og fjerner data for
slettede eller manglende `file_id`. Se
`devel-docs/database-v17-migration.md`.

## Dagens versjon

Dagens schema er `OPENCLIP_SCHEMA_VERSION = 1` i `bildebank/openclip.py`.
Versjonen lagres i:

```text
meta.schema_version
```

Schema v1 har disse tabellene:

- `meta`
- `image_embeddings`
- `image_search_runs`
- `image_search_results`

## Kompatibilitet med uversjonerte databaser

OpenCLIP-databaser fra før schema-versjonering kan mangle
`meta.schema_version`. De aller eldste kan også mangle hele `meta`-tabellen.

Et slikt schema adopteres som v1 bare når:

- alle tre datatabellene finnes
- alle kolonnene runtime-koden trenger finnes
- samlingsinterne `target_path`-verdier er relative
- `PRAGMA foreign_key_check` ikke finner brutte deklarerte referanser
- `PRAGMA integrity_check` er `ok`

Adopsjonen skjer under `BEGIN IMMEDIATE`. Koden oppretter bare `meta` hvis den
mangler og setter `meta.schema_version=1`. Datatabellene bygges ikke om, og
embedding- eller søkeresultatrader endres ikke. Derfor lages det ikke en egen
backup for denne metadataendringen.

Ved feil rulles hele metadataendringen tilbake. Et mangelfullt uversjonert
schema avvises uten at manglende tabeller eller kolonner opprettes lydløst.

## Validering

Ved vanlig åpning av en v1-database kontrolleres:

- alle forventede tabeller og nødvendige kolonner
- relative samlingsinterne stier
- `PRAGMA foreign_key_check`

En nyere eller ukjent eksplisitt schema-versjon avvises uten endringer.

Full `PRAGMA integrity_check` kjøres når en ny database opprettes eller et
uversjonert schema adopteres, men ikke ved hver ordinære åpning av en allerede
gjeldende database.

`unimport` validerer eller adopterer OpenCLIP-schemaet før databasen festes til
hovedtransaksjonen. Dermed stopper et ukjent eller mangelfullt schema før
bildesamlingen endres.

## Sikker skanning

`image-scan` bruker bare aktive, relative `files.target_path`-verdier uten
`..`. Originalen åpnes uten å følge symlinker eller Windows reparse points og
holdes åpen mens Pillow og OpenCLIP behandler den. Pillow kontrollerer
dimensjonene mot den felles pikselgrensen før full dekoding.

Før en embedding lagres, tar koden target-låsen på nytt og kontrollerer både
databaseposten og originalfilens identitet, størrelse og endringstid.
Resultatet forkastes hvis filen ble byttet, endret, fjernet eller markert
slettet mens modellen arbeidet. En intern skanneidentitet hindrer også en
eldre skanning i å skrive etter at en nyere er startet.

## Reparasjon av kopierte embedding-stier

`repair-image-search-paths` reparerer bare utdaterte `target_path`- og
`target_path_key`-felt i `image_embeddings`. Den reparerer ikke SHA-avvik,
søkeresultater, orphan-rader eller mediefiler.

Reparasjonen krever samme aktive `file_id` og SHA-256 i hoveddatabasen og
OpenCLIP-databasen. Den hasher også den faktiske mediefilen stabilt og krever
at både størrelse og SHA-256 stemmer med `files`. Hoveddatabasens stier,
begge databasenes schema og databasehelse samt `pending_file_moves`
kontrolleres før endring.

Standardkjøringen er dry-run. `--apply` holder target-låsen, bygger planen på
nytt og oppdaterer bare de to kopierte sti-feltene i én transaksjon. Embedding-
blobber og `updated_at` endres ikke. Det tas ikke automatisk en full kopi av
OpenCLIP-databasen, fordi den kan være svært stor og feltene er avledet direkte
fra tre identiske identitetsbevis. Et oppdatert snapshot anbefales likevel før
`--apply`.

## Regler for senere schema-endringer

- Øk `OPENCLIP_SCHEMA_VERSION`.
- Lag en eksplisitt migrering fra forrige versjon.
- Ikke bygg om eller slett embeddings uten en egen sikkerhetsvurdering.
- Bevar gamle embeddings når det er praktisk.
- Kjør migrering og validering i én transaksjon.
- Skriv regresjonstester for rollback, databevaring og gjeldende schema.

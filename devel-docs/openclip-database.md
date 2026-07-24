# OpenCLIP-database

OpenCLIP-data lagres separat fra hoveddatabasen i:

```text
.bilder-openclip.sqlite3
```

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

## Regler for senere schema-endringer

- Øk `OPENCLIP_SCHEMA_VERSION`.
- Lag en eksplisitt migrering fra forrige versjon.
- Ikke bygg om eller slett embeddings uten en egen sikkerhetsvurdering.
- Bevar gamle embeddings når det er praktisk.
- Kjør migrering og validering i én transaksjon.
- Skriv regresjonstester for rollback, databevaring og gjeldende schema.

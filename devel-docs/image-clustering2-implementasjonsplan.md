# Implementeringsplan for Leiden-gruppering

Status: implementert til og med automatiserte tester. Punkt 7 under
verifikasjon gjenstår på nativ Windows og den faktiske bildesamlingen.

## Beslutninger

- Bruk `igraph==1.0.0` og `Graph.community_leiden()` direkte. Egen
  `leidenalg`-pakke er ikke nødvendig.
- Bruk CPM, `n_iterations=-1`, `beta=0.01` og en eksplisitt seed gjennom
  igraphs RNG-adapter.
- Bruk deterministisk, chunket NumPy-matrisemultiplikasjon for eksakt kNN.
  Diagonalen fjernes etter matriseindeks, og likhet brytes med `file_id`.
- Bruk foreløpig CPM-oppløsning `0.2`. Verdien må kalibreres manuelt på den
  faktiske samlingen før funksjonen regnes som ferdig verifisert.
- Ikke lagre grafkanter. Hver kjøring bygger sin egen graf.
- Alle isolerte bilder lagres i én noise-gruppe. En helt kantløs graf
  fullføres med `actual_cluster_count=0`.

## OpenCLIP-schema v3

Legg følgende nullable felt til `image_clustering_runs`:

- `input_fingerprint TEXT`
- `input_fingerprint_version INTEGER CHECK(... IS NULL OR ... > 0)`
- `effective_neighbor_count INTEGER CHECK(... IS NULL OR ... >= 0)`
- `graph_node_count INTEGER CHECK(... IS NULL OR ... >= 0)`
- `graph_edge_count INTEGER CHECK(... IS NULL OR ... >= 0)`
- `isolated_file_count INTEGER CHECK(... IS NULL OR ... >= 0)`
- `threshold_removed_edge_count INTEGER CHECK(... IS NULL OR ... >= 0)`
- `nearest_similarity_median REAL CHECK(... IS NULL OR (... >= -1 AND ... <= 1))`
- `kth_similarity_median REAL CHECK(... IS NULL OR (... >= -1 AND ... <= 1))`
- `library_versions_json TEXT`

Migreringen v2–v3 er additiv, kjører i den eksisterende transaksjonen og
endrer ingen gamle rader. V1 migreres først til v2 og deretter til v3.
Read-only krever fortsatt gjeldende schema og migrerer aldri.

## Domeneimplementering

1. Legg til validert `LeidenParameters` med stabile JSON-verdier.
2. Beregn SHA-256-fingerprint versjon 1 over modellnøkkel, dimensjon, sorterte
   `file_id`-er og little-endian normaliserte `float32`-rader.
3. Finn eksakte naboer i avgrensede chunks. For hver rad velges høyeste
   likhet, med `file_id` som tie-break.
4. Bygg urettede `union`- eller `mutual`-kanter, bruk terskel og valgfri
   cosinusvekt, og beregn grafstatistikk.
5. Ta isolerte noder ut av igraph-inputen. Kjør Leiden på resten med lokal,
   seedet RNG.
6. Beregn normalisert gjennomsnittsembedding og cosinusavstand for hver vanlig
   gruppe. Noise rangeres etter `file_id`.
7. Skriv grupper, medlemmer, fingerprint, statistikk og bibliotekversjoner i
   den eksisterende atomiske slutt-transaksjonen.

## Integrasjon

- `bildebank/image_clustering.py`: parametere, fingerprint, kNN, graf, Leiden
  og lagring.
- `bildebank/openclip.py`: schema v3, migrering og validering.
- `bildebank/cli.py` og `bildebank/cli_image.py`: interne workerargumenter og
  parameterbygging.
- `bildebank/launcher_commands.py`: ren Leiden-argumentbygger.
- `bildebank/launcher_widgets.py`: algoritmeavhengige Leiden-felt og lokal
  validering.
- `bildebank/launcher_tools_tab.py`: videresend verdiene og forklar
  avhengigheten.
- `bildebank/server_endpoints_clustering.py`: navn, nøkkelparametere og
  grafstatistikk.
- `pyproject.toml`, dependency-status og dokumentasjon: legg igraph i
  OpenCLIP-profilen.

## Tester og verifikasjon

1. Schemaoppretting, v1–v3, v2–v3, rollback, read-only og gamle nullable runs.
2. Parametervalidering, fingerprint, eksakte naboer, duplikater, union,
   mutual, terskel og statistikk.
3. Syntetiske Leiden-grafer, alle isolerte, små grupper, determinisme og
   atomisk lagring.
4. Worker-, kommandobygger-, launcher- og webtester.
5. Regresjon for MiniBatchKMeans, HDBSCAN og sidecar-livssyklus.
6. Ruff, pyflakes, mypy og full pytest-suite.
7. Nativ Windows-generering av alle dependency-låser, installasjonstest og
   manuell kalibrering på omtrent 20 000 bilder gjenstår som plattformtest.

# repair-image-search-paths

<!-- CLI-HELP-START -->
```text
usage: bildebank repair-image-search-paths [valg]

Vis eller synkroniser utdaterte stier i OpenCLIP embeddings når file_id,
SHA-256 og mediefilen fortsatt stemmer med hoveddatabasen.

options:
  -h, --help  show this help message and exit
  --apply     Oppdater reparerbare target_path- og target_path_key-felt.
```
<!-- CLI-HELP-END -->

Kommandoen kan rette utdaterte filstier i databasen for tekstbasert bildesøk.
Den endrer ikke bilder, hoveddatabasen eller selve embeddingene.

Kjør først en dry-run:

```powershell
bildebank repair-image-search-paths
```

Bildebank viser bare rader som kan repareres uten å velge mellom motstridende
data. `file_id` og SHA-256 må stemme mellom hoveddatabasen og
OpenCLIP-databasen. Den faktiske mediefilen må også ha riktig størrelse og
SHA-256.

Ta et oppdatert snapshot før du bruker `--apply`. Kjør deretter:

```powershell
bildebank repair-image-search-paths --apply
```

Apply oppdaterer bare `target_path` og `target_path_key` i reparerbare
`image_embeddings`-rader. SHA-avvik, søkeresultater, orphan-rader og
mediefiler blir ikke endret. Hvis en fil har endret innhold, en database ikke
er frisk eller en filflytting er uavklart, stopper kommandoen uten å reparere
embedding-stier.

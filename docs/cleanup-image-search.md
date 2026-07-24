# cleanup-image-search

<!-- CLI-HELP-START -->
```text
usage: bildebank cleanup-image-search [valg]

Vis eller slett OpenCLIP-rader som peker på filer som mangler i hoveddatabasen
eller er markert som slettet.

options:
  -h, --help  show this help message and exit
  --apply     Slett foreldreløse image_embeddings og image_search_results.
```
<!-- CLI-HELP-END -->

Kommandoen rydder i databasen for tekstbasert bildesøk. Den sletter ikke
bildefiler.

Ny `remove`-opprydding sletter normalt bildesøkdata samtidig som et bilde
flyttes til `deleted`. Denne kommandoen kan fortsatt rydde eldre rader og rader
som er blitt foreldreløse av andre årsaker.

Standard er dry-run:

```powershell
bildebank cleanup-image-search
```

Da viser Bildebank hvor mange foreldreløse `image_embeddings` og
`image_search_results` som finnes, og noen eksempler på `file_id` og sti.

For å slette de foreldreløse radene:

```powershell
bildebank cleanup-image-search --apply
```

Kommandoen sletter bare bildesøk-rader som peker på filer som ikke finnes i
hoveddatabasen, eller filer som er markert som slettet. Aktive bilder beholdes.
Tomme gamle søkekjøringer slettes også.

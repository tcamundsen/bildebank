# make-face-browser
<!-- CLI-HELP-START -->
```text
usage: bildebank make-face-browser [valg]

Debug-verktøy. Denne kommandoen lager faces.html for kontroll av scannede
ansikter, men er ikke ment for vanlig bruk.

options:
  -h, --help           show this help message and exit
  --limit LIMIT        Maks antall bilder som tas med. Anbefales fordi siden
                       kan bli svært stor.
  -o, --output OUTPUT  Skriv HTML-filen hit. Standard: faces.html i
                       bildesamlingen.
```
<!-- CLI-HELP-END -->

> [!WARNING]
> `make-face-browser` er et debug-verktøy. Det er ikke ment for vanlig bruk.

Kommandoen lager en HTML-side for scannede ansikter, men siden kan bli svært
stor hvis bildesamlingen har mange ansikter. **Så den må omskrives** Bruk den
når du vil slå opp `face-id` eller kontrollere ansiktsmarkeringer.

Standardfilen heter `faces.html`. Den viser bilder der Bildebank har funnet
ansikter, med boks rundt ansiktene og ansikt-id.

## Valg

### `--limit`

Bestemmer hvor mange bilder som tas med:

```powershell
bildebank make-face-browser --limit 50
```

Uten `--limit` kan siden inneholde svært mange bilder.

### `--output`

Angi filnavn til generert fil.

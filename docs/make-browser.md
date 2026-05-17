# make-browser
<!-- CLI-HELP-START -->
```text
usage: bildebank make-browser [valg]

options:
  -h, --help            show this help message and exit
  -o, --output OUTPUT   Skriv HTML-filen hit. Standard: index.html i
                        bildesamlingsmappen.
  --month-preview-limit MONTH_PREVIEW_LIMIT
                        Maks antall filer i månedsoversikten. Standard: vis
                        alle.
```
<!-- CLI-HELP-END -->

`make-browser` skriver en HTML-fil med alle aktive bilder og videoer i
bildesamlingen. Standardfilen heter `index.html` og legges i
bildesamlingsmappen.

Browseren kan bla mellom bilder og videoer, hoppe mellom måneder og år, og vise
en enkel månedsoversikt.

Hvis du importerer flere filer senere, må du kjøre `make-browser` på nytt for å
oppdatere HTML-filen.

For vanlig arbeid i bildesamlingen er [`run-server`](run-server.md) anbefalt.

## Valg

### `--month-preview-limit`

Begrenser hvor mange filer som vises i månedsoversikten:

```powershell
bildebank make-browser --month-preview-limit 40
```

### `--output`

`-o` eller `--output` skriver HTML-filen til et annet filnavn:

```powershell
bildebank make-browser -o "familiebilder.html"
```

## Hurtigtaster

Når HTML-filen er åpen i nettleseren, kan du bla med tastaturet:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde eller video |
| Pil høyre | Neste bilde eller video |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |

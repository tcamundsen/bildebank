# make-browser

`make-browser` lager en enkel statisk bildebrowser som HTML-fil. Den kan åpnes
direkte i nettleseren uten at Bildebank kjører.

For vanlig arbeid i bildesamlingen er [`run-server`](run-server.md) anbefalt.

## Referanse

```powershell
bildebank make-browser [valg]
```

Vanlige valg:

```powershell
bildebank make-browser
bildebank make-browser --month-preview-limit 40
bildebank make-browser -o "familiebilder.html"
```

## Hva kommandoen gjør

Kommandoen skriver en HTML-fil med alle aktive bilder og videoer i
bildesamlingen. Standardfilen heter `index.html` og legges i
bildesamlingsmappen.

Browseren kan bla mellom bilder og videoer, hoppe mellom måneder og år, og vise
en enkel månedsoversikt.

Hvis du importerer flere filer senere, må du kjøre `make-browser` på nytt for å
oppdatere HTML-filen.

## Valg

`--month-preview-limit` begrenser hvor mange filer som vises i månedsoversikten:

```powershell
bildebank make-browser --month-preview-limit 40
```

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

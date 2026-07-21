# make-browser
<!-- CLI-HELP-START -->
```text
usage: bildebank make-browser [valg]

options:
  -h, --help            show this help message and exit
  -o, --output OUTPUT   Skriv HTML-filen hit. Standard: index.html i
                        bildesamlingsmappen.
  --hide-out-of-focus   Ikke ta med bilder tagget "Ute av fokus" i den
                        statiske HTML-browseren.
  --month-preview-limit MONTH_PREVIEW_LIMIT
                        Maks antall filer i månedsoversikten. Standard: vis
                        alle.
```
<!-- CLI-HELP-END -->

`make-browser` skriver en statisk HTML-fil med alle aktive bilder, videoer og
andre importerte bildefiler i bildesamlingen. Standardfilen heter `index.html` og legges i
bildesamlingsmappen.

Google/Pixel motion-videoer skjules fra de vanlige måneds- og årslistene når de
hører til et bilde med samme navn. De er fortsatt bevart og kan vises med
filtersøk, for eksempel `type:video`, `extension:mp4` eller
`filename:PXL_20250102`.

Browseren kan bla mellom filer, hoppe mellom måneder og år, og vise oversikter
for år, måneder og filer. Stien øverst, for eksempel
`År / 2024 / Januar / IMG_1234.jpg`, kan brukes til å gå tilbake til en
oversikt. RAW/NEF og PSD vises som fil-lenker, ikke som bildevisning.

AVI-videoer bruker en eksisterende MP4-avspillingskopi når den finnes. Den
statiske browseren beholder samtidig en lenke til den originale AVI-filen. Kjør
[`make-video-previews`](make-video-previews.md) før `make-browser` hvis slike
kopier mangler.

Bildene vises med visningsrotasjonen som er lagret i Bildebank. Rotasjonen
gjøres bare i HTML-browseren. Selve bildefilene blir ikke endret.

Kommentarer vises nederst på mediet i full filvisning. De vises ikke på års-
eller månedsoversiktene. Kjør `make-browser` på nytt etter at en kommentar er
endret, slik at HTML-filen får den nye teksten.

Hvis du importerer flere filer senere, må du kjøre `make-browser` på nytt for å
oppdatere HTML-filen.

Ideen bak å lage en statistk HTML-fil er at den kan brukes hvis man har
bildesamlingen på en ekstern disk og vil vise bildene på en PC som ikke har
Bildebank installert.  For vanlig arbeid i bildesamlingen er
[`run-server`](run-server.md) anbefalt.

## Valg

### `--month-preview-limit`

Begrenser hvor mange filer som vises i månedsoversikten:

```powershell
bildebank make-browser --month-preview-limit 40
```

### `--hide-out-of-focus`

Lager HTML-filen uten bilder som er tagget `Ute av fokus`:

```powershell
bildebank make-browser --hide-out-of-focus
```

I Bildebank-vinduet er dette valget checkboxen `Skjul "Ute av fokus"` ved
knappen `Lag HTML-browser`.

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

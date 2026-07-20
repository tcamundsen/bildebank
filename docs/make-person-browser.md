# make-person-browser
<!-- CLI-HELP-START -->
```text
usage: bildebank make-person-browser [valg] navn

positional arguments:
  navn                  Personnavn

options:
  -h, --help            show this help message and exit
  -o, --output OUTPUT   Skriv HTML-filen hit. Standard: person-Navn.html i
                        bildesamlingen.
  --month-preview-limit MONTH_PREVIEW_LIMIT
                        Maks antall bilder i månedsoversikten. Standard: vis
                        alle.
  --hide-out-of-focus   Ikke ta med bilder tagget "Ute av fokus" i den
                        statiske personbrowseren.
```
<!-- CLI-HELP-END -->

`make-person-browser` lager en HTML-fil for å bla i bilder der Bildebank tror en
bestemt person finnes.

Denne kommandoen krever at ansiktsgjenkjenning er aktivert og tatt i bruk.
Se [InsightFace](web/insightface.md).

Siden viser bilder der personen enten er:

- bekreftet med `face-person-add-face`
- foreslått med `face-suggest`

Selve HTML-browseren er enkel og viser bildene uten ansiktsbokser eller
redigeringsfunksjoner. Bruk [`run-server`](run-server.md) hvis du vil bekrefte
ansikter eller arbeide videre med personforslag. Tanken er at den skal kunne brukes
til å vise bilder på PC-er som ikke har Bildebank installert.

Stien øverst, for eksempel `År / 2024 / Januar / IMG_1234.jpg`, kan brukes til
å gå tilbake til oversikter for år, måneder og filer.

Bildene vises med visningsrotasjonen som er lagret i Bildebank. Bildefilene
endres ikke.

Kommentarer vises nederst på bildet i full bildevisning, men ikke på
oversiktsbildene. Kjør `make-person-browser` på nytt etter at en kommentar er
endret.

## Valg

### `--month-preview-limit`

Begrenser hvor mange filer som vises i månedsoversikten:

```powershell
bildebank make-person-browser "Tom" --month-preview-limit 40
```

### `--hide-out-of-focus`

Lager HTML-filen uten bilder som er tagget `Ute av fokus`:

```powershell
bildebank make-person-browser "Tom" --hide-out-of-focus
```

### `--output`

`-o` eller `--output` skriver HTML-filen til et annet filnavn:

```powershell
bildebank make-person-browser "Tom" -o "tom.html"
```

## Hurtigtaster

Når HTML-filen er åpen i nettleseren, kan du bla med tastaturet:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde |
| Pil høyre | Neste bilde |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |


## Oppdatere HTML-filen

Hvis du scanner flere bilder, kjører `face-suggest`, eller bekrefter flere
ansikter, må du kjøre `make-person-browser` på nytt for å oppdatere HTML-filen.

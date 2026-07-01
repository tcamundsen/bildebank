# make-people-browser
<!-- CLI-HELP-START -->
```text
usage: bildebank make-people-browser [valg]

Lag HTML-index og personside for alle registrerte personer

options:
  -h, --help            show this help message and exit
  --month-preview-limit MONTH_PREVIEW_LIMIT
                        Maks antall bilder i månedsoversikten på hver
                        personside. Standard: vis alle.
  --hide-out-of-focus   Ikke ta med bilder tagget "Ute av fokus" i de statiske
                        personbrowserne.
```
<!-- CLI-HELP-END -->

`make-people-browser` lager statiske HTML-sider med `make-person-browser` for alle
personer som er registrert i ansiktsdatabasen samt filen `personer.html` som
lenker alle person-sidene.

Personoversikten og personsidene følger visningsrotasjonen som er lagret i
Bildebank. Bildefilene endres ikke.

## Valg

### `--month-preview-limit`

Begrenser hvor mange filer som vises i månedsoversikten:

```powershell
bildebank make-people-browser --month-preview-limit 40
```

### `--hide-out-of-focus`

Lager personoversikten og personsidene uten bilder som er tagget `Ute av fokus`:

```powershell
bildebank make-people-browser --hide-out-of-focus
```


## Oppdatere HTML-filen

Hvis du scanner flere bilder, kjører `face-suggest`, eller bekrefter flere
ansikter, må du kjøre `make-people-browser` på nytt for å oppdatere HTML-filene.

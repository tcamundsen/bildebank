# make-person-browser

`make-person-browser` lager en HTML-fil for å bla i bilder der Bildebank tror en
bestemt person finnes.

Dette er en del av den eksperimentelle ansiktsgjenkjenningen.

## Referanse

```powershell
bildebank make-person-browser "Navn"
```

Vanlige valg:

```powershell
bildebank make-person-browser "Kari"
bildebank make-person-browser "Kari" --month-preview-limit 40
bildebank make-person-browser "Kari" -o "kari.html"
```

## Hva kommandoen gjør

Kommandoen lager en HTML-fil i bildesamlingen. Standardfilen får navn etter
personen, for eksempel:

```text
person-Kari.html
```

Siden viser bilder der personen enten er:

- bekreftet med `face-person-add-face` eller `face-person-add-group`
- foreslått med `face-suggest`

Bekreftede ansikter og forslag markeres med ulik farge på boksen rundt
ansiktet.

## Før du bruker kommandoen

Du må først ha scannet bilder:

```powershell
bildebank face-scan
```

Deretter må personen finnes i ansiktsdatabasen. Det kan gjøres ved å bekrefte
et ansikt. Personen må opprettes først:

```powershell
bildebank face-person-create "Kari"
bildebank face-person-add-face "Kari" 798
```

Når noen ansikter er bekreftet, kan Bildebank lage forslag:

```powershell
bildebank face-suggest
```

Da kan `make-person-browser` vise både bekreftede bilder og foreslåtte bilder.

## Bla i personbrowseren

Personbrowseren viser ett bilde om gangen. Dette gjør at den kan brukes selv om
personen finnes i mange bilder.

Når du hopper til en annen måned eller et annet år, viser siden først en
månedsoversikt. Klikk på et bilde i oversikten for å åpne det.

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

## Valg

`--month-preview-limit` begrenser hvor mange bilder som vises i
månedsoversikten:

```powershell
bildebank make-person-browser "Kari" --month-preview-limit 40
```

Dette kan gjøre månedsoversikten lettere å bruke hvis personen finnes i mange
bilder i samme måned.

`-o` eller `--output` skriver HTML-filen til et annet filnavn:

```powershell
bildebank make-person-browser "Kari" -o "kari.html"
```

## Oppdatere HTML-filen

Hvis du scanner flere bilder, kjører `face-suggest`, eller bekrefter flere
ansikter, må du kjøre `make-person-browser` på nytt for å oppdatere HTML-filen.

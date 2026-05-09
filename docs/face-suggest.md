# face-suggest

`face-suggest` foreslår personer for ukjente ansikter.

## Referanse

```powershell
bildebank face-suggest
bildebank face-suggest --threshold 0.70
bildebank face-suggest --no-browser
```

Forslagene bygger på ansikter du allerede har bekreftet med
`face-person-add-face` eller `face-person-add-group`.

Høyere `--threshold` gir strengere forslag. Forslagene er ikke bekreftede før
du selv kobler ansiktet til personen.

Som standard oppdaterer kommandoen også `personer.html` og personsidene, slik
at du kan åpne resultatet i nettleseren med en gang.

Se også [`Strategier for face-suggest`](face-suggest-strategier.md) for råd om
hvordan du bør velge ansikter som skal bekreftes.

Hvis du bare vil beregne forslag uten å skrive HTML-filene, bruker du:

```powershell
bildebank face-suggest --no-browser
```

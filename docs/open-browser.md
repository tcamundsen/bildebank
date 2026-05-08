# open-browser

`open-browser` åpner en HTML-browser som allerede er laget med `make-browser`.

## Referanse

```powershell
bildebank open-browser [valg]
```

Vanlige valg:

```powershell
bildebank open-browser
bildebank open-browser --file "familiebilder.html"
```

## Hva kommandoen gjør

Som standard åpner kommandoen `index.html` i bildesamlingsmappen.

Hvis filen ikke finnes, må du lage den først:

```powershell
bildebank make-browser
```

Deretter kan du åpne den:

```powershell
bildebank open-browser
```

## Åpne en annen HTML-fil

Hvis du laget browseren med `-o` eller `--output`, kan du åpne den filen med
`--file`:

```powershell
bildebank make-browser -o "bare-video.html" --media video
bildebank open-browser --file "bare-video.html"
```

## Hurtigtaster

Hurtigtastene virker i HTML-filen etter at den er åpnet i nettleseren:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde eller video |
| Pil høyre | Neste bilde eller video |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |


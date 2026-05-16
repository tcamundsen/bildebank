# make-browser

`make-browser` lager en statisk bildebrowser i form av en HTML-fil
for å bla i bildesamlingen i nettleseren.

## Referanse

```powershell
bildebank make-browser [valg]
```

Vanlige valg:

```powershell
bildebank make-browser
bildebank make-browser --month-preview-limit 40
bildebank make-browser --media image
bildebank make-browser --media video
bildebank make-browser --date-source metadata
bildebank make-browser -o "familiebilder.html"
```

## Hva kommandoen gjør

Kommandoen lager en HTML-fil med oversikt over importerte bilder og videoer.
Standardfilen heter `index.html` og legges i bildesamlingsmappen.

Hvis du importerer flere filer senere, må du kjøre `make-browser` på nytt for å
oppdatere HTML-filen.

Sjekk om ansiktsgjenkjenning finnes i den statiske browseren. Tror egentlig
iokke vi vil ha det.

Hvis ansiktsgjenkjenning er aktivert, viser bildebrowseren også personer som er
koblet til bildet. Navnene vises nederst ved filnavnet. Du kan klikke på et
navn for å åpne personsiden, for eksempel `person-Kari.html`.

Forslag fra `face-suggest` vises med teksten `(forslag)`. Det betyr at
Bildebank tror personen kan være i bildet, men at du ikke har bekreftet det
selv.

Hvis bildet er scannet for ansikter, viser browseren også knappen `Ansikter i
bildet`. Den åpner en detaljvisning med ett markert ansikt om gangen, `face-id`
og en kommando du kan kopiere. Dette er nyttig hvis du ser et bilde der du vet
hvem personen er, men Bildebank ikke har gjenkjent personen ennå.

Eksempel på kommandoen som kan kopieres:

```powershell
bildebank face-person-add-face "Navn" 1234
```

Bytt ut `Navn` med riktig personnavn før du kjører kommandoen.

## Valg

`--month-preview-limit` begrenser hvor mange filer som vises i månedsoversikten.
Dette kan gjøre HTML-filen lettere å bruke hvis du har mange bilder:

```powershell
bildebank make-browser --month-preview-limit 40
```

`--media` kan brukes for å lage en browser med bare bilder eller bare videoer:

```powershell
bildebank make-browser --media image
bildebank make-browser --media video
```

`--date-source` kan brukes for å vise filer etter hvilken dato Bildebank brukte:

```powershell
bildebank make-browser --date-source metadata
bildebank make-browser --date-source filename
bildebank make-browser --date-source mtime
bildebank make-browser --date-source unknown
```

`-o` eller `--output` skriver HTML-filen til et annet filnavn:

```powershell
bildebank make-browser -o "bare-video.html" --media video
```

## Hurtigtaster i browseren

Når HTML-filen er åpen i nettleseren, kan du bla med tastaturet:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde eller video |
| Pil høyre | Neste bilde eller video |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |

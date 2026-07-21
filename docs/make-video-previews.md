# make-video-previews

<!-- CLI-HELP-START -->
```text
usage: bildebank make-video-previews [valg]

Lag regenererbare MP4-kopier av aktive AVI- og 3GP-videoer for nettleseren.

options:
  -h, --help     show this help message and exit
  --dry-run      Vis hva som mangler uten å installere programmer eller skrive
                 filer.
  --limit LIMIT  Maks antall AVI- og 3GP-filer som skal kontrolleres.
  --verbose      Vis filer som feiler.
  --rebuild      Lag alle AVI- og 3GP-avspillingskopier på nytt.
```
<!-- CLI-HELP-END -->

Nettlesere kan ha problemer med å spille av AVI- og 3GP-filer direkte. Kommandoen
lager derfor MP4-avspillingskopier som Bildebank kan bruke i nettleseren.
Originalfilene blir ikke endret, flyttet eller slettet.

Du kan gjøre dette fra Bildebank-vinduet med knappen
**Lag videoavspilling**, eller fra PowerShell:

```powershell
bildebank make-video-previews
```

Første kjøring kan ta lang tid. Bildebank hopper senere over kopier som allerede
finnes. Avspillingskopiene ligger under `video-previews\v1` i bildesamlingen.
De kan regenereres og tas derfor ikke med i snapshots.

FFmpeg og FFprobe installeres automatisk i Bildebank-programmappen på Windows.
Hvis installasjonen tidligere har feilet, prøver Bildebank igjen ved neste
oppstart. Du kan også reparere installasjonen med
[`ffmpeg-install`](ffmpeg-install.md).

## Kontrollere først

Denne kommandoen viser hvor mange avspillingskopier som mangler, uten å laste
ned programmer eller skrive filer:

```powershell
bildebank make-video-previews --dry-run
```

Bruk `--limit 10` for å kontrollere eller konvertere opptil ti AVI- og 3GP-filer.
`--verbose` viser navnene på filer som ikke kunne konverteres. Én feil stopper
ikke de øvrige filene. Kommandoen avslutter med exit-code `2` hvis minst én fil
feilet.

`--rebuild` lager alle AVI- og 3GP-avspillingskopiene på nytt. Den ferdige kopien får
ikke sitt endelige navn før FFmpeg-jobben er fullført og resultatet er
kontrollert.

# face-scan

<!-- CLI-HELP-START -->
```text
usage: bildebank face-scan [valg]

Scanner importerte bilder etter ansikter.

options:
  -h, --help           show this help message and exit
  --limit LIMIT        Maks antall bildefiler som skal sjekkes
  --force              Scan valgte bilder på nytt selv om de allerede er
                       scannet
  --show-model-output  Vis intern output fra InsightFace/ONNX ved feilsøking
```
<!-- CLI-HELP-END -->

`face-scan` scanner importerte bilder og lagrer matematiske beskrivelser av
ansikter `.bilder-faces.sqlite3`.

Den hopper over bilder som allerede er scannet med samme innhold. Det er trygt
å avbryte med `Ctrl-C`; neste kjøring fortsetter ved å hoppe over det som er
ferdig.

Med `--force` scanner den valgte bildene på nytt. Gamle ansikter, forslag og
personkoblinger for disse bildene fjernes før nye ansikter lagres.

Hvis en fil feiler under scanning, skriver `face-scan` filstien og
feilmeldingen. Feilen kan også ses senere med:

```powershell
bildebank face-report
```

## Valg

### `--limit ANTALL`

Skann maksimalt ANTALL bilder. Fint å bruke for å teste på et mindre antall bilder først.

### `--force`

Scan valgte bilder på nytt selv om de allerede er scannet:

```powershell
bildebank face-scan --force --limit 10
```

### `--show-model-output`

Viser intern output fra InsightFace/ONNX ved feilsøking.

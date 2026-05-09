# face-scan

`face-scan` scanner importerte bilder etter ansikter.

## Referanse

```powershell
bildebank face-scan
bildebank face-scan --limit 100
bildebank face-scan --show-model-output
```

Kommandoen lagrer ansikter og embeddings i `.bilder-faces.sqlite3`.

Den hopper over bilder som allerede er scannet med samme innhold. Det er trygt
å avbryte med `Ctrl-C`; neste kjøring fortsetter ved å hoppe over det som er
ferdig.

`--limit` kan brukes for å teste på et mindre antall bilder først.

`--show-model-output` viser intern output fra InsightFace/ONNX ved feilsøking.

Hvis en fil feiler under scanning, skriver `face-scan` filstien og
feilmeldingen. Feilen kan også ses senere med:

```powershell
bildebank face-report
```

Se også [`insightface`](insightface.md).

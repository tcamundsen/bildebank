# InsightFace

InsightFace er en valgfri testkomponent for ansiktsgjenkjenning.

Vanlig Bildebank-installasjon installerer ikke InsightFace, og
ansiktsgjenkjenning er av som standard.

## Installere testkomponenten

Kjør dette fra programmappen:

```powershell
.\install-insightface.ps1
```

Scriptet installerer InsightFace og ONNX Runtime i Bildebanks lokale
Python-miljø i `.venv`. Det installerer ikke noe som et vanlig Windows-program.

Scriptet lager også `bildebank-config.toml` hvis filen mangler.

## Config

Utgangspunktet ligger i:

```text
bildebank-config.example.toml
```

Den lokale filen heter:

```text
bildebank-config.toml
```

Den lokale filen skal ikke legges i Git.

For testing kan den se slik ut:

```toml
[face_recognition]
enabled = false
provider = "cpu"
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
```

`enabled = false` betyr at Bildebank ikke bruker ansiktsgjenkjenning.

## Status

Sjekk status med:

```powershell
bildebank face-status
```

Kommandoen viser om config er av eller på, hvor modellene skal ligge, og om
`insightface` og `onnxruntime` er installert.

## Modeller

InsightFace kan laste ned modeller første gang det brukes. Bildebank skal bruke
modellmappen fra config, slik at modellene havner i programmappen og ikke
spres andre steder.

De ferdigtrente modellene fra InsightFace er oppgitt som kun for
ikke-kommersiell forskning. Dette må avklares før funksjonen brukes til noe mer
enn lokal testing.

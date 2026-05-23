# face-config

<!-- CLI-HELP-START -->
```text
usage: bildebank face-config true|false

Slå ansiktsgjenkjenning på eller av

positional arguments:
  true|false  true slår på ansiktsgjenkjenning, false slår den av

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`face-config` slår ansiktsgjenkjenning på eller av i `bildebank-config.toml`.

Eksempel:

```powershell
bildebank face-config true
bildebank face-config false
```

Bruk `true` for å slå på ansiktsgjenkjenning og `false` for å slå den av.
Kommandoen oppretter `bildebank-config.toml` hvis filen mangler.

Når `bildebank run-server` kjører, kan du også slå dette på og av fra
`Innstillinger`-siden i nettleseren. Hvis du slår ansiktsgjenkjenning av
ellerpå mens `run-server` kjører, så må du avbryte med Ctrl-C og kjøre
`bildebank run-server` på nytt.

Se status etterpå:

```powershell
bildebank face-status
```

Se også [`insightface`](insightface.md).

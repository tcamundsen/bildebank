# face-config

`face-config` slår ansiktsgjenkjenning på eller av i `bildebank-config.toml`.

## Referanse

```powershell
bildebank face-config true
bildebank face-config false
```

Bruk `true` for å slå på ansiktsgjenkjenning og `false` for å slå den av.
Kommandoen oppretter `bildebank-config.toml` hvis filen mangler.

Når `bildebank run-server` kjører, kan du også slå dette på og av fra
`Innstillinger`-siden i nettleseren.

Se status etterpå:

```powershell
bildebank face-status
```

Se også [`insightface`](insightface.md).

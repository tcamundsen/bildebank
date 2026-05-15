# geo-stats

`geo-stats` viser hvor mange aktive bilder i bildesamlingen som er scannet for
GPS-koordinater.

```powershell
bildebank geo-stats
```

Kommandoen viser blant annet:

- totalt antall aktive bilder
- hvor mange som er scannet for GPS
- hvor mange som har GPS-koordinater
- hvor mange som mangler GPS-koordinater
- hvor mange som har GPS-feil

Slettede bilder, altså bilder som er flyttet til `deleted/`, telles ikke.

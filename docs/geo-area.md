# geo-area

`geo-area` lister aktive bilder i én H3-celle.

Bruk først `geo-areas` for å finne en H3-celle:

```powershell
bildebank geo-areas
```

List deretter bildene i cellen:

```powershell
bildebank geo-area 871ec91b2ffffff
```

Vanlige valg:

- `--limit N` begrenser hvor mange bilder som vises.
- `--with-date` viser dato fra Bildebank-databasen.
- `--with-coordinates` viser GPS-koordinater.

Eksempel:

```powershell
bildebank geo-area 871ec91b2ffffff --with-date --with-coordinates
```

Slettede bilder, altså bilder som er flyttet til `deleted/`, vises ikke.

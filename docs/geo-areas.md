# geo-areas

`geo-areas` grupperer bilder med GPS-koordinater etter H3-celler.

```powershell
bildebank geo-areas
bildebank geo-areas --resolution 7
bildebank geo-areas --resolution 8 --min-count 5
```

Vanlige valg:

- `--resolution N` velger H3-oppløsning. Tillatte verdier er 5, 6, 7, 8 og 9.
  Standard er 7.
- `--min-count N` viser bare områder med minst N bilder. Standard er 2.
- `--limit N` begrenser hvor mange områder som vises. Standard er 50.

Kommandoen viser bare H3-celle og antall bilder. Den prøver ikke å finne
stedsnavn.

Slettede bilder, altså bilder som er flyttet til `deleted/`, telles ikke.

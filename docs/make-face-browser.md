# make-face-browser

`make-face-browser` er et debug-verktøy. Det er ikke ment for vanlig bruk.

Kommandoen lager en HTML-side for scannede ansikter, men siden kan bli svært
stor hvis bildesamlingen har mange ansikter. Bruk vanligvis `face-group` og
`face-groups.html` i stedet.

## Referanse

```powershell
bildebank make-face-browser --limit 100
bildebank make-face-browser -o "faces.html"
```

Standardfilen heter `faces.html`. Den viser bilder der Bildebank har funnet
ansikter, med boks rundt ansiktene og ansikt-id.

`--limit` bestemmer hvor mange bilder som tas med:

```powershell
bildebank make-face-browser --limit 50
```

Uten `--limit` kan siden inneholde svært mange bilder.

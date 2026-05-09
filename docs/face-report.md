# face-report

`face-report` viser rapport for scannede ansikter.

## Referanse

```powershell
bildebank face-report
bildebank face-report --limit 50
```

Rapporten viser antall scannede filer, antall ansikter, filer med flere
ansikter og eventuelle scan-feil.

Den viser også personstatus:

- hvor mange personer som er registrert
- hvor mange ansikter som er bekreftet koblet til personer
- hvor mange forslag som finnes
- hvor mange bilder som har minst én bekreftet person
- hvor mange bilder som har ansikter, men ingen bekreftet person
- hvor mange bilder som har både bekreftede og ukjente ansikter

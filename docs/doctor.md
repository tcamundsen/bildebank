# doctor
<!-- CLI-HELP-START -->
```text
usage: bildebank doctor [valg]

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`doctor` viser en read-only diagnose for Bildebank-installasjonen og aktiv
bildesamling. Kommandoen endrer ikke config, databaser eller bildefiler.

Kommandoen skriver korte linjer:

- `OK:` betyr at dette ser riktig ut.
- `OBS:` betyr at noe er av, mangler eller ikke er gjort ennå, men at det ikke
  nødvendigvis er en feil.
- `FEIL:` betyr at noe brukeren har slått på eller trenger, ikke virker.
- `Råd:` viser hva du bør gjøre videre.

`doctor` sjekker blant annet config-fil, H3, ExifTool, ansiktsgjenkjenning,
tekstbasert bildesøk og aktiv bildesamling. Hvis for eksempel
`face_recognition` er slått på, men InsightFace mangler eller ikke kan lastes,
viser kommandoen en `FEIL:`-linje og et råd om hva du bør gjøre videre.

Det gamle navnet `face-status` virker fortsatt som alias.

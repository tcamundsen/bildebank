# doctor
<!-- CLI-HELP-START -->
```text
usage: bildebank doctor [valg]

options:
  -h, --help  show this help message and exit
  --deep      Kjør tregere filintegritetssjekker.
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
tekstbasert bildesøk, aktiv bildesamling og om databaseførte bildefiler
faktisk finnes i samlingen. Hvis for eksempel
`face_recognition` er slått på, men InsightFace mangler eller ikke kan lastes,
viser kommandoen en `FEIL:`-linje og et råd om hva du bør gjøre videre.

`bildebank doctor --deep` leser i tillegg alle aktive filer og kontrollerer
SHA-256 mot databasen. Denne kontrollen kan ta lang tid for en stor samling.
Den rapporterer feil, men endrer ikke databasen eller filene.

Det gamle navnet `face-status` virker fortsatt som alias.

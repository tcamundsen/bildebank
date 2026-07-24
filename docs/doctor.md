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
faktisk finnes i samlingen. Den leter også etter mediefiler i samlingen som
ikke har noen databasepost, og rapporterer dem som orphan-filer. Hvis
OpenCLIP-databasen for bildesøk finnes, sjekker den også om bildesøkdata peker
på filer som mangler i hoveddatabasen eller er markert som slettet. Hvis for eksempel
`face_recognition` er slått på, men InsightFace mangler eller ikke kan lastes,
viser kommandoen en `FEIL:`-linje og et råd om hva du bør gjøre videre.

Før doctor vurderer de databaseførte filene, kontrollerer den at
hoveddatabasen er hel og at databasereferanser ikke peker på rader som mangler.
Hvis databasefilens integritet ikke kan bekreftes, hopper doctor over senere
filkontroller. Da unngår kommandoen å gi en sikkerhetsvurdering basert på en
database som kan være skadet.

Doctor kontrollerer også at alle databaseførte filer, inkludert filer under
`deleted`, har registrert kildeinformasjon. SHA-256 og filstørrelse skal være
like i filraden og den tilhørende kildeinformasjonen. Avvik rapporteres, men
repareres ikke.

`bildebank doctor --deep` leser i tillegg alle aktive filer og kontrollerer
SHA-256 mot databasen. Denne kontrollen kan ta lang tid for en stor samling.
Doctor viser fremdrift mens den leter etter manglende filer, scanner etter
orphan-filer og kontrollerer SHA-256. Den rapporterer feil, men endrer ikke
databasen eller filene.

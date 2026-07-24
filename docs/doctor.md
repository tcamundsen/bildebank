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
på filer som mangler i hoveddatabasen eller er markert som slettet. Databasen
må ha gjeldende, eksplisitt OpenCLIP-schema. For aktive filer må kopiert
filsti, stinøkkel og SHA-256 stemme med hoveddatabasen. Avvik rapporteres, men
doctor migrerer, rydder eller regenererer ikke OpenCLIP-data. Hvis for eksempel
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

Doctor rapporterer også uavklarte filflyttinger og filer som står i kø for
sletting etter `unimport`. En fil i slettingskøen må ha en trygg samlingssti,
ikke lenger være referert av databasen og ha samme størrelse og SHA-256 som da
den ble lagt i køen. Derfor leser doctor innholdet i disse køfilene også uten
`--deep`. En endret eller utrygg fil rapporteres som feil og røres ikke.
Doctor behandler aldri køene automatisk.

Databaseførte samlingsstier må være relative og bruke `/` som skilletegn.
Doctor avviser blant annet absolutte stier, `..`, feil mappeplassering,
`target_path_key` som ikke stemmer, og stier som går gjennom en symlink,
junction eller et annet Windows reparse point. Hvis stiene ikke kan bekreftes
som trygge, åpner eller hasher doctor ikke databaseførte bildefiler i samme
kjøring.

Vanlig doctor kontrollerer både aktive filer og filer under `deleted`. Hver
databaseført fil må finnes som en vanlig fil uten lenker, og størrelsen på
disk må stemme med `files.size_bytes`. Denne kontrollen leser filinformasjon,
men ikke selve filinnholdet. Unntaket er ekstra filer i slettingskøen, som må
hashes for å kunne avgjøre om innholdsidentiteten fortsatt stemmer.

`bildebank doctor --deep` leser i tillegg alle aktive filer og alle filer
under `deleted`, og kontrollerer SHA-256 mot databasen. Filen må være den samme
vanlige filen gjennom hele lesingen; hvis den byttes eller endres underveis,
blir den ikke godkjent. Denne kontrollen kan ta lang tid for en stor samling.
Doctor viser fremdrift mens den leter etter manglende filer, scanner etter
orphan-filer og kontrollerer SHA-256. Den rapporterer feil, men endrer ikke
databasen eller filene.

# Bildesorteringsprogram

## Formål

Det har blitt vanskelig å holde oversikt over digitale bilder fordi de ligger
spredt på flere enheter og i flere mapper. Samlingen inneholder også mange
duplikater fra mobiltelefoner, digitalkameraer, USB-minnepinner og gamle
sikkerhetskopier.

Programmet skal samle bilder fra flere kildemapper i en felles målmappe, uten
å lagre samme bilde flere ganger. Målmappen skal organiseres etter år og
måned basert på når bildene er tatt.

Programmet skal være konservativt: Det er viktigere å få med alle bilder og
videoer enn å fjerne absolutt alle mulige duplikater. Det er bedre å importere
en ekstra kopi enn å risikere at et unikt bilde eller en unik video går tapt.

Programmet skal aldri slette, flytte eller endre filer i kildemappene. Import
skal bare kopiere filer fra kildemapper til målmappen.

## Begreper

- **Kildemappe**: En mappe programmet skal scanne etter bilder.
- **Målmappe**: Mappen der programmet legger den samlede og ryddede
  bildesamlingen.
- **Eksakt duplikat**: En fil som har samme innhold som en fil som allerede
  finnes i målmappen, selv om filnavn eller plassering kan være forskjellig.
- **Importert kilde**: En kildemappe eller et flyttbart medium som programmet
  tidligere har behandlet og registrert i databasen.
- **Duplikatfunn**: En kildefil som ikke kopieres fordi programmet finner en
  eksakt duplikat i målmappen.
- **Udatert fil**: Et bilde eller en video der programmet ikke klarer å finne
  dato fra metadata, filens endringsdato eller filnavn.
- **Databasen**: En fil i målmappen som holder oversikt over importerte bilder, 
  importerte kilder, filhash, duplikatfunn, feil og kommandologg.

## Eksempelbruk

Opprette en målmappe. Dette er første kommando som kan kjøres. Alle
andre skal feile hvis denne ikke er utført:

    $ bdb target /path/to/target/bilder

Denne kommandoen oppretter målmappen og initierer en databasefil. Databasen
skal blant annet inneholde kontroll med:

 - registrering av kildemapper
 - info om alle bildene i målmappen, for å kunne sjekke etter duplikater.
 - logg over alle kommandoer som har blitt kjørt.

Etter at databasen har blitt opprettet, så krever scriptet `bdb` enten
at det kjøres fra målmappen, eller at målmappen angis med
kommandolinjeparameteren `--target=/path/to/målmappe`

Legge en mappe til listen over kildemapper:

    $ bdb add /path/to/directory

Scanne alle registrerte kildemapper:

    $ bdb import

`bdb import` skal vise progresjon underveis, for eksempel ved å vise hvor mange bilder
som er scannet, og hvor mange som er importert. Scriptet skal tåle å bli avbrutt
med ctrl-C. Dette gjøres ved at en kildemappe bare kan markeres som importert i databasen når
hele importen er gjennomført.

Databasen bør oppdateres og lagres periodisk underveis i importen, for
eksempel etter hver 200. importerte fil. Programmet trenger ikke gjøre en egen
database-commit for hvert eneste bilde.

Hvis brukeren trykker ctrl-C, skal programmet forsøke å stoppe kontrollert:
fullføre eventuell pågående filkopiering, skrive siste databaseendringer og
deretter avslutte. Hvis programmet avbrytes hardt før siste database-commit, er
det akseptabelt at neste kjøring må gjøre litt ekstraarbeid. Programmet skal da
kunne oppdage filer som allerede ligger i målmappen, og unngå å lage duplikater.

Kopiering skal gjøres på en måte som hindrer halvkopierte filer i målmappen.
Programmet bør kopiere til en midlertidig fil i riktig målmappe, verifisere at
hash på målfilen matcher hash på kildefilen, og deretter gi filen endelig navn.
Filen skal først registreres som importert i databasen etter vellykket kopiering
og verifisering.

Når bildene (og videoene) importeres, så skal ikke filnavnet deres endres.
Ved navnekollisjon i samme måned, så legges "-1", "-2" etc til filnavnet,
før filendelsen, for eksempel `IMG1324-2.jpg`. Samtidig må det markeres
i databasen at dette bildet har fått lagt til "-1" på grunn av navnekollisjon.
Kommando for å liste bilder med navnekollisjon:

    $ bdb list-name-conflicts

Legge til og scanne flyttbare medier, som CD-ROM eller usb-brikke. Siden
det er flyttbare medier, så kan neste scan scanne et annet medium på
samme path:

    $ bdb import-removable --name="cd-2005" /path/to/media

`--name` er etiketten som er skrevet på CD-rom, eller en annen måte for
brukeren å identifisere kilden på.

For å liste opp alle kildemapper, og id for alle flyttbare medier som er 
lagt til:

    $ bdb list-sources

For å vise oppsummering av siste import eller hele databasen:

    $ bdb report

Rapporten bør vise antall importerte filer, duplikatfunn, navnekollisjoner,
filer uten dato og feil.

## Om flyttbare medier

Flyttbare medier, som CD-ROM, minnepinner og eksterne disker, må behandles
annerledes enn vanlige kildemapper. Grunnen er at samme mount path kan brukes
for forskjellige medier på forskjellige tidspunkt.

Kommandoen `import-removable` scanner derfor en konkret kilde én gang og
registrerer den med en brukerdefinert etikett. Etiketten bør være noe brukeren
kan kjenne igjen senere, for eksempel teksten som står skrevet på en CD-ROM.

Etter at scanningen er ferdig, antar ikke programmet at samme medium vil være
tilgjengelig igjen på samme path.

Siden noen medier kan være skrivebeskyttet, kan ikke programmet lagre en unik
ID på selve mediet. Programmet forutsetter derfor at brukeren bruker `--name`
konsekvent for å identifisere samme medium senere.

## Teknologi

Programmet skal skrives i Python. Planen er at dette skal være et program
som utelukkende kjøres fra kommandolinjen. Det er høy prioritet å garantere
at alle unike bilder fra alle kildemapper som importeres blir med i 
målmappen.

Databasen bør være SQLite. SQLite gir transaksjoner, indekser og trygg lokal
lagring uten å kreve en separat databaseserver.

## Plattform

Utvikling kan gjøres i WSL Debian, men programmet skal kjøres nativt i
Windows 11. Implementasjonen må derfor være plattformuavhengig og ikke bygge på
Linux-spesifikke filsystemantakelser. Python-versjon som er tilgjengelig i
WSL nå er 3.13.5. På windows er 3.14.3 tilgjengelig.

Programmet skal bruke Python-biblioteker som fungerer godt på Windows, for
eksempel `pathlib` for filstier, `shutil` for filkopiering og `sqlite3` for
databasen. Koden skal håndtere Windows-stier, drive letters, mellomrom i
filnavn, Unicode i filnavn og at Windows-filsystemer vanligvis ikke skiller på
store og små bokstaver i filnavn.

Flyttbare medier på Windows kan få ulike drive letters eller mount points fra
gang til gang. `--name` er derfor fortsatt nødvendig for å gi et flyttbart
medium en stabil brukerdefinert identitet.

Enhetstester kan kjøres i WSL under utvikling, men før programmet tas i bruk på
den faktiske bildesamlingen må importflyten testes i Windows 11 med ekte
Windows-stier og et lite testsett med bilder og videoer.

Repo-/prosjektmappen kan hete `bilder`, og Python-pakken kan også hete
`bilder`. CLI-kommandoen kan fortsatt hete `bdb`. Python-pakken bør ikke hete
`bdb`, fordi `bdb` også er navnet på en modul i Pythons standardbibliotek.

Programmet bør deles i tydelige deler:

- lesing og validering av database og kommandolinjeargumenter
- scanning av kildemapper
- lesing av bildedato og metadata
- duplikatkontroll
- kopiering til riktig år- og månedsmappe
- lagring og lesing av database for importerte kilder

## Målmappe

Målmappen skal inneholde alle unike bilder som er kopiert inn fra
kildemappene.

Målmappen skal ikke ligge inni en kildemappe, og en kildemappe skal ikke ligge
inni målmappen. Programmet skal avvise slike oppsett for å unngå at målmappen
importeres inn i seg selv.

Mappestrukturen i målmappen skal være:

```text
målmappe/
  2023/
    01/
    02/
  2024/
    07/
```

Det skal bare opprettes mapper for år og måneder der programmet faktisk finner
bilder.

Bilder og videoer uten kjent dato skal likevel importeres. De legges i en egen
mappe, for eksempel:

```text
målmappe/
  udatert/
```

## Importmodell

Målmappen skal inneholde en database som registrerer hvilke kildemapper som
allerede er scannet og importert. Denne databasen brukes til å unngå at samme
kildemappe behandles flere ganger når programmet kjøres på nytt.

Typisk arbeidsflyt:

```bash
$ bdb target /path/to/target/bilder
$ bdb add /path/folder/with/images
$ bdb import
$ bdb add /path/to/more/images
$ bdb import
```

Etter den andre `bdb add` i eksempelet over, så skal programmet se i databasen
at den mappen som ble lagt til først allerede er importert, og bare importere
bilder fra den andre mappen.

Hvis en kilde inneholder filer som ikke kan leses, skal feilen registreres i
databasen og vises i rapporten. En kilde skal ikke markeres som problemfritt
importert hvis noen filer feilet under import.

## Duplikathåndtering

Programmet skal unngå kjente eksakte duplikater. Første versjon skal bruke
filhash, for eksempel SHA-256, til å avgjøre om to filer er like.

Programmet skal ikke bruke visuell likhet, perceptual hash eller andre
usikre metoder for automatisk å slå sammen filer i første versjon. Når målet er
å unngå tap, er det bedre å importere noen ekstra filer enn å feilaktig forkaste
en unik fil.

Når programmet finner et eksakt duplikat, skal kildefilen ikke kopieres på nytt.
Databasen skal likevel registrere duplikatfunnet med original kildepath og
hvilken fil i målmappen den matcher. På den måten kan brukeren senere se at
filen faktisk ble funnet og vurdert.

## Filformater, dato og feil

- Hvilke bildefilformater skal støttes? I hvert fall JPEG. Hvis det dukker opp
  andre bildeformater, så legges det til støtte etter hvert.
- Videoer behandles sammen med bilder, og legges i mappe basert på 
  når filmen ble tatt opp
- Dato hentes fra metadata i bildet hvis det finnes. Hvis ikke, må man se på om
  filens endringsdato eller filnavn gir informasjon.
- Hvordan skal programmet rapportere feil, for eksempel utilgjengelige mapper
  eller filer som ikke kan leses? Første utgave av programmet kan skrive om
  dette til stdout og registrere feilen i databasen.

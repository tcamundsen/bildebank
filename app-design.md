# Bildesorteringsprogram

## Formål

Det har blitt vanskelig å holde oversikt over digitale bilder fordi de ligger
spredt på flere enheter og i flere mapper. Samlingen inneholder også mange
duplikater fra mobiltelefoner, digitalkameraer, USB-minnepinner og gamle
sikkerhetskopier.

Programmet skal samle bilder fra flere kildemapper i en felles målmappe, uten
å lagre samme bilde flere ganger. Målmappen skal organiseres etter år og
måned basert på når bildene er tatt.

## Begreper

- **Kildemappe**: En mappe programmet skal scanne etter bilder.
- **Målmappe**: Mappen der programmet legger den samlede og ryddede
  bildesamlingen.
- **Duplikat**: Et bilde som allerede finnes i målmappen, selv om filnavn eller
  plassering kan være forskjellig.
- **Importert mappe**: En kildemappe som programmet tidligere har behandlet og
  registrert i målmappens datafil.
- **Databasen**: En fil i målmappen som holder oversikt over importerte bilder, 
  importerte kilder, filhash og kommandologg.

## Eksempelbruk

Opprette en målmappe. Dette er første kommando som kan kjøres. Alle
andre skal feile hvis denne ikke er utført:

    $ bdb target /path/to/target/bilder

Denne kommandoen oppretter målmappen og initierer en som databasefil. Databasen
skal blant annet inneholde kontroll med:

 - registrering av kildemapper
 - info om alle bildene i målmappen, for å kunne sjekke her etter duplikat.
 - logg over alle kommandoer som har blitt kjørt.

Etter at databasen har blitt opprettet, så krever scriptet `bdb` enten
at det kjøres fra målmappen, eller at målmappen angis med command line
parameter `--target=/path/to/målmappe`

Legge en directory til listen over kildemapper:

    $ bdb add /path/file/directory

Scanne alle registrerte kildemapper:

    $ bdb import

`bdb import` skal vise progresjon underveis, for eksempel ved å vise hvor mange bilder
som er scannet, og hvor mange som er importert. Scriptet skal tåle å bli avbrutt
med ctrl-C. Dette gjøres ved at en kildemappe bare kan markeres som importert i databasen når
hele importen er gjennomført.

Databasen bør oppdateres og committes periodisk underveis i importen, for
eksempel etter hver 200. importerte fil. Programmet trenger ikke gjøre en egen
database-commit for hvert eneste bilde.

Hvis brukeren trykker ctrl-C, skal programmet forsøke å stoppe kontrollert:
fullføre eventuell pågående filkopiering, skrive siste databaseendringer og
deretter avslutte. Hvis programmet avbrytes hardt før siste database-commit, er
det akseptabelt at neste kjøring må gjøre litt ekstraarbeid. Programmet skal da
kunne oppdage filer som allerede ligger i målmappen, og unngå å lage duplikater.

Når bildene (og videoene) importeres, så skal ikke filnavnet deres endres.
Ved navnekollisjon i samme måned, så legges "-1", "-2" etc til filnavnet,
før filendelsen, for eksempel `IMG1324-2.jpg`. Samtidig må det markeres
i databasen at dette bildet har fått lagt til "-1" pga navnekollisjon. Kommando
for å liste bilder med navnekollisjon:

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

## Importmodell

Målmappen skal inneholde en datafil som registrerer hvilke kildemapper som
allerede er scannet og importert. Denne datafilen brukes til å unngå at samme
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

## Duplikathåndtering

Målmappen skal ikke inneholde duplikater. Programmet må derfor kunne avgjøre
om et bilde allerede finnes i målmappen før det kopieres inn. Dette gjøres
ved at databasen lagrer hash på alle importerte bilder og videoer.

## Åpne avklaringer

- Hvilke bildefilformater skal støttes? I hvert fall jpeg. Hvis det dukker opp
  andre bildeformater, så legges det til støtte etter hvert.
- Videoer behandles sammen med bilder, og legges i mappe basert på 
  når filmen ble tatt opp
- Dato hentes fra metadata i bildet hvis det finnes. Hvis ikke, må man se på om
  filens endringsdato eller filnavn gir informasjon.
- Hva skal skje hvis et bilde mangler dato? I første utgave av programmet, så kan
  `bdb import` skrive info om det til stdout.
- Hvordan skal programmet rapportere feil, for eksempel utilgjengelige mapper
  eller filer som ikke kan leses? Første utgave av programmet kan skrive om
  dette til stdout.

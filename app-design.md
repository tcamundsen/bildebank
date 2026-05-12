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

## Planlagte brukerfunksjoner

Det er noen brukerfunksjoner som er planlagt, men ikke på plass ennå. Dette
gjelder blant annet muligheten til å angre på en import.

Browseren bør senere komme i en ny utgave med mulighet for å slette bilder.
Det bør også vurderes om bilder kan samles i en underkategori for en måned, slik
at alle bildene fra en bestemt hendelse kan grupperes, for eksempel julaften
eller en ferieuke. Det må vurderes hvordan ferieuker som dekker deler av to
måneder skal håndteres.

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

    $ bildebank target /path/to/target/bilder

Denne kommandoen oppretter målmappen og initierer en databasefil. Databasen
skal blant annet inneholde kontroll med:

- registrering av navngitte kilder
 - info om alle bildene i målmappen, for å kunne sjekke etter duplikater.
 - logg over alle kommandoer som har blitt kjørt.

Etter at databasen har blitt opprettet, så krever scriptet `bildebank` enten
at det kjøres fra målmappen, eller at målmappen angis med
kommandolinjeparameteren `--target=/path/to/målmappe`
Brukeren bør alltid kjøre `bildebank` fra målmappen for å gjøre et enkelt for seg.

Importere en kilde direkte:

    $ bildebank import --name "sommer2024" /path/to/source
    $ bildebank import --name "sommer2024" --dry-run /path/to/source
    $ bildebank import --name "sommer2024" --dry-run --log-file=importliste.txt /path/to/source

`bildebank import` skal vise progresjon underveis, for eksempel ved å vise hvor mange bilder
som er scannet, og hvor mange som er importert. Scriptet skal tåle å bli avbrutt
med ctrl-C. Dette gjøres ved at en kilde bare kan markeres som importert i databasen når
hele importen er gjennomført.

Med `--dry-run` skal programmet bare liste filer som ville blitt importert.
Det skal ikke kopiere filer, opprette målmapper, registrere importerte filer,
registrere duplikatfunn, logge importkommandoen eller markere kilder som
importert. Med `--log-file` skrives dry-run-listen til fil.

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

Kopieringen skal fungere på vanlige filsystemer som brukes på Windows, eksterne
disker og Linux, for eksempel NTFS, exFAT, FAT32, SMB/nettverksmapper og ext4.
Programmet skal derfor ikke være avhengig av filsystemfunksjoner som ikke er
universelt tilgjengelige, for eksempel hardlinks. Midlertidig fil bør ligge i
samme mappe som endelig målfil slik at endelig rename/flytting skjer innenfor
samme filsystem.

Når bildene (og videoene) importeres, så skal ikke filnavnet deres endres.
Ved navnekollisjon i samme måned, så legges "-1", "-2" etc til filnavnet,
før filendelsen, for eksempel `IMG1324-2.jpg`. Samtidig må det markeres
i databasen at dette bildet har fått lagt til "-1" på grunn av navnekollisjon.
Kommando for å liste bilder med navnekollisjon:

    $ bildebank list-name-conflicts

Kommando for å undersøke én navnekollisjon nærmere:

    $ bildebank show-name-conflict /path/to/target/2017/08/img3331.jpg

Hvis filen er del av en navnekollisjon, viser kommandoen alle filene i samme
konfliktgruppe med både målfil og opprinnelig kildefil. Kommandoen skal også
fungere hvis brukeren peker på den første filen i gruppen, altså filen som ikke
fikk `-1`, `-2` eller tilsvarende suffix.

Flyttbare medier, som CD-ROM eller usb-brikke, importeres med samme kommando.
Siden samme path kan brukes av forskjellige medier på forskjellige tidspunkt,
er `--name` identiteten til importen:

    $ bildebank import --name="cd-2005" /path/to/media
    $ bildebank import --name="cd-2005" --dry-run /path/to/media

Med `--dry-run` vises filene som ville blitt importert, uten å registrere
kilden, kopiere filer eller endre databasen.

For å liste opp alle registrerte kilder:

    $ bildebank list-sources

For å reversere en tidligere import:

    $ bildebank unimport --dry-run --name "cd-2005"
    $ bildebank unimport --name "cd-2005"

`unimport` skal gjøre samlingen slik den ville vært hvis denne importen aldri
hadde skjedd. Dette er en destruktiv kommando og må derfor være ekstra
konservativ. Programmet skal først finne den registrerte kilden som matcher
navnet, og kontrollere at alle kildefilene som ble registrert for denne kilden
fortsatt finnes i kilden, har samme filstørrelse og samme SHA-256 som da de ble
importert eller registrert som duplikat. `unimport` skal alltid kjøres med
`--name`, både for vanlige mapper og flyttbare medier. Hvis én eneste fil
mangler eller har endret innhold, skal kommandoen avbrytes uten å endre
databasen eller målmappen. Kravet finnes fordi en ny import av samme kilde må
være mulig etter en unimport, slik at ingen bilder går tapt.

Når kontrollen er godkjent, skal programmet vise hvilken kilde som blir
reversert, hvor mange målfilreferanser som fjernes fra samlingen, og hvor mange
av disse filene som fortsatt blir liggende i målmappen fordi samme fil også er
registrert fra andre kilder. Hvis en målfil bare har denne kilden som
proveniens, fjernes målfilen fra den aktive samlingen. Hvis målfilen også har
andre `file_sources`, skal selve målfilen ikke slettes; bare henvisningen til
kilden som unimporteres fjernes. Etterpå skal `make-browser` og
`open-browser` vise bildene som om denne importen aldri hadde skjedd.

Før noe endres skal brukeren bekrefte med en nøyaktig tekst:

```text
Kilde: /path/to/source
Filer som fjernes fra aktiv samling: 142
Filer som blir liggende fordi de også finnes i andre kilder: 37
Skriv "ja, det vil jeg" for å gjennomføre unimport:
```

Bare svaret `ja, det vil jeg` skal gjennomføre kommandoen. Alle andre svar,
inkludert tomt svar, skal avbryte uten endringer.

Med `--dry-run` skal programmet gjøre alle kontroller og vise samme oppsummering,
men ikke spørre om bekreftelse, ikke slette filer, ikke endre databasen og ikke
skrive kommandologg.

Når `unimport` kjøres, skal source-raden fjernes helt etter vellykket
reversering. Den skal ikke bli stående som `pending`.

For å vise oppsummering av siste import eller hele databasen:

    $ bildebank report
    $ bildebank status

Rapporten bør vise antall importerte filer, duplikatfunn, navnekollisjoner,
filer uten dato og feil.

`bildebank status` viser antall importerte filer fordelt på bilder og videoer, og
hvor mange filer som er plassert basert på metadata, filnavn, mtime eller
manglende dato.

For å liste filer som ble plassert basert på noe annet enn metadata:

    $ bildebank non-metadata
    $ bildebank non-metadata --with-source

Kommandoen viser filer der datoen kom fra filnavn, filens endringsdato eller
manglende dato. Med `--with-source` vises også den opprinnelige kildefilen.

For å se hvilken kilde en importert målfil kommer fra:

    $ bildebank show-source /path/to/target/2010/09/image.jpg

Kommandoen viser målfil, opprinnelig kildefil, kilde-id, kildetype, registrert
kilde, importdato, valgt dato/datokilde, filstørrelse og SHA-256.

For å fjerne en importert fil fra den aktive samlingen uten å slette den
permanent:

    $ bildebank delete 2007/02/filename.png
    $ bildebank list-deleted

Kommandoen flytter filen til tilsvarende sti under `deleted/`, for eksempel
`deleted/2007/02/filename.png`. Raden beholdes i databasen, men markeres som
slettet med tidspunkt og opprinnelig målsti. Slettede filer skal ikke tas med i
`make-browser`. `bildebank list-deleted` viser filer som er markert som slettet, hvor
de opprinnelig lå, hvor de ble flyttet, og om filen fortsatt finnes på disk.

Programmet er ikke avhengig av at filene under `deleted/` blir liggende for å
bruke den aktive samlingen videre. Brukeren kan derfor slette filer under
`deleted/` manuelt når de er ferdig kontrollert. Databasen beholder fortsatt
historikken, men programmet kan naturligvis ikke lenger vise, åpne eller
gjenopprette selve filinnholdet hvis filen er fysisk slettet fra `deleted/`.

For å sammenligne programmet med en lokal ExifTool-installasjon:

    $ bildebank exiftool-metadata-gaps

Kommandoen forventer som standard `exiftool.exe` i målmappen. Den kjører
ExifTool på filer der `bildebank` ikke har funnet metadata-dato, og viser filer der
ExifTool finner en datotag som programmet fortsatt ikke leser. Kommandoen skal
bare brukes som diagnosehjelp for å forbedre metadata-støtten.

For å forklare hvilken dato programmet ville brukt for én enkelt fil:

    $ bildebank explain-date /path/to/file.jpg
    $ bildebank inspect-metadata /path/to/file.jpg

Kommandoen viser valgt dato, valgt datokilde og hvilke datokandidater
programmet fant i metadata, filnavn og filens endringsdato.
`inspect-metadata` viser i tillegg metadatafragmenter og tekstlige datotreff
for å kunne undersøke filer der programmet ikke finner dato automatisk.

Når programmet får bedre metadata-støtte, skal det være mulig å sjekke filer
som tidligere ble plassert uten metadata på nytt:

    $ bildebank refresh-metadata
    $ bildebank refresh-metadata --dry-run
    $ bildebank refresh-metadata --verbose

Kommandoen går gjennom filer der `date_source` ikke er `metadata`, leser
metadata på nytt og flytter filen inne i målmappen hvis den nå kan plasseres
bedre. Dette påvirker bare filer i målmappen, aldri filer i kildemappene.
Med `--dry-run` vises bare en oppsummering uten at filer flyttes eller databasen
endres. Med `--verbose` vises filer som flyttes, hoppes over eller feiler.

For å liste feil som er registrert i databasen:

    $ bildebank errors
    $ bildebank errors --stage refresh-metadata
    $ bildebank errors --include-resolved

Kommandoen viser uløste feil som standard. Feil som programmet senere har
reparert markeres som løst og skjules fra standardvisningen.
`--include-resolved` viser også løste historiske feil. Dette gjør at brukeren kan
undersøke aktive feil uten å bruke `sqlite3` direkte.

For å lage en statisk HTML-browser i målmappen:

    $ bildebank make-browser
    $ bildebank open-browser

`make-browser` skriver `index.html` i målmappen. HTML-filen inneholder en
innebygd indeks fra databasen og kan derfor åpnes direkte i nettleseren uten
mappevelger. Den viser bilder og videoer med relative paths fra målmappen, men
kopierer ikke bilde- eller videofilene. `open-browser` åpner som standard
`index.html` i standard nettleser. Med `--file` kan den åpne en annen HTML-fil
i målmappen. Browseren kan filtreres på medietype og datokilde når den lages:

    $ bildebank make-browser --media=image
    $ bildebank make-browser --media=video
    $ bildebank make-browser --date-source=metadata
    $ bildebank make-browser --date-source=filename
    $ bildebank make-browser --date-source=mtime

Filtrene kan kombineres, for eksempel for å lage en HTML-browser med bare
videoer som er plassert basert på metadata:

    $ bildebank make-browser --media=video --date-source=metadata

Månedsoversikten viser som standard alle filer i måneden. Hvis det senere viser
seg å bli tungt på enkelte maskiner eller i svært store måneder, kan brukeren
sette et tak:

    $ bildebank make-browser --month-preview-limit=40

Senere kan programmet få en lokal interaktiv browser:

    $ bildebank serve

`serve` skal være en egen arbeidsmodus, ikke en erstatning for den statiske
HTML-filen. Den starter en lokal webserver som viser en browser med samme
grunnleggende navigasjon som `index.html`: år, måneder, bilder, videoer og
filtre. Forskjellen er at nettleseren da kan sende handlinger tilbake til
Bildebank-programmet. Trinn 1 bør være å kunne bla i bildene omtrent som i den
statiske browseren, åpne ett bilde, og fjerne bildet man ser på. Fjerning skal
bruke samme semantikk som `bildebank remove`: filen flyttes til `deleted/`,
databasen markeres, og kildemappen røres ikke. Serveren bør tilby én HTML/app,
API for liste over filer eller en valgt måned, media-URL-er for filvisning og et
POST-endepunkt for fjerning. Dermed kan `make-browser` fortsatt være en trygg
lesevisning, mens `serve` blir modusen for arbeid som endrer samlingen.

For å lage en statisk HTML-side for manuell gjennomgang av navnekollisjoner:

    $ bildebank make-conflict-browser

Kommandoen skriver `name-conflicts.html` i målmappen. Siden lar brukeren bla
mellom navnekollisjonene og viser filene i hver konflikt side om side, sammen
med målfil, kildefil, kilde-id, dato, datokilde, oppløsning, filstørrelse,
SHA-256 og om kildefilen fortsatt finnes.

## Om flyttbare medier

Flyttbare medier, som CD-ROM, minnepinner og eksterne disker, kan få samme
mount path som et annet medium senere. Derfor skal alle importer ha `--name`.
Navnet er brukerens stabile identitet for kilden, uansett om kilden er en
vanlig mappe eller et flyttbart medium.

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

## Database v4

Databasen ligger i målmappen som `.bilder.sqlite3`. I schema v4 er proveniens
normalisert slik at én målfil kan ha flere kildefilforekomster. Dette er
nødvendig for å kunne vite forskjell på "denne filen finnes bare i én kilde"
og "denne målfilen er bevist av flere kilder med samme innhold".

Nye databaser som opprettes med schema v4 skal bruke disse tabellene:

- `meta`: nøkkel/verdi-tabell for database-metadata. Den viktigste verdien er
  `schema_version`, som skal være `4` for v4-databaser.
- `command_log`: logg over kommandoer som har endret eller forsøkt å endre
  databasen.
- `sources`: registrerte kilder. Alle kilder har `name TEXT NOT NULL UNIQUE`,
  path, importstatus og eventuell superseding av eldre underkilder. Det finnes
  ikke lenger en egen `kind`-kolonne for vanlig mappe kontra flyttbart medium.
- `files`: målfilene i samlingen. Hver rad beskriver én fil som finnes eller
  har funnet sted i målmappen: målsti, lagret filnavn, originalt filnavn,
  SHA-256, filstørrelse, valgt dato, datokilde, navnekollisjon og eventuell
  slettemarkering.
- `file_sources`: kildefilforekomstene som beviser målfilene. Hver rad peker
  på én `files`-rad og én `sources`-rad, og inneholder kildefilens path,
  normalisert path-nøkkel, SHA-256 og filstørrelse. Det finnes ikke lenger en
  `kind`-kolonne; duplikater/proveniens følger av at én `files`-rad kan ha
  flere `file_sources`-rader.
- `errors`: feil som er oppdaget under scanning, import eller senere
  vedlikehold. Feil kan senere markeres som løst med `resolved_at`.

Viktige v4-regler:

- `files` er sannheten om målfilene i samlingen.
- `file_sources` er sannheten om hvilke kilder som peker på hver målfil.
- En målfil kan ha mange `file_sources`.
- En kildefilforekomst identifiseres unikt med `(source_id, source_path_key)`.
- Runtime-kode skal lese proveniens fra `file_sources`, ikke fra gamle
  provenienskolonner på `files`.
- Nye duplikater skal registreres som flere `file_sources`-rader som peker på
  samme `files`-rad.

Eldre skjemavarianter håndteres av `bildebank migrate`. Den historiske
migreringen til v4 beskrives i `devel-docs/database-v4-migration.md`.

## Plattform

Utvikling kan gjøres i WSL Debian, men programmet skal kjøres nativt i
Windows 11. Implementasjonen må derfor være plattformuavhengig og ikke bygge på
Linux-spesifikke filsystemantakelser. Python-versjon som er tilgjengelig i
WSL nå er 3.13.5. På Windows er 3.14.3 tilgjengelig.

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

Prosjektet heter nå `bildebank`. Se etter rester fra tidligere der det
har het `bdb` eller `bilder`. De bør helst endres til `bildebank` om mulig.

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

Målmappen skal ikke ligge inni programrepoet, for eksempel under
`$HOME/kode/bildebank`. Programmet skal avvise dette når brukeren kjører
`bildebank create target-dir`, slik at testbilder, importerte bilder, database og generert
HTML ikke blandes med programkode og Git-status.

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
$ bildebank create /path/to/target/bilder
$ bildebank import --name "bilder-1" /path/folder/with/images
$ bildebank import --name "bilder-2" /path/to/more/images
```

Hver import har et unikt navn. Programmet bruker navnet til senere kommandoer
som `unimport`.

Hvis en overmappe importeres etter at en undermappe allerede er importert, skal
programmet behandle dette som overlappende kilder. Identiske filer skal ikke
kopieres på nytt, men den nye importen får egne `file_sources`-rader for filene
den også inneholder. Da kan brukeren senere kjøre `unimport` på den første
underimporten uten at bildene forsvinner, så lenge de også finnes i overmappen.

En vanlig kildemappe behandles som en avsluttet importjobb, ikke som en mappe
som senere synkroniseres automatisk.

Det er fortsatt lov å registrere en overmappe etter at en undermappe allerede er
importert, slik at man kan gå fra en liten testimport til en større import. Når
overmappen er ferdig importert uten feil, markeres den tidligere undermappen som
`superseded`.

Hvis det senere blir behov for å scanne en tidligere importert kilde om igjen,
bør det være en egen eksplisitt kommando, for eksempel `bildebank rescan-source ID`,
slik at brukeren tydelig ber om en ny gjennomgang.

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
- Dato hentes fra metadata i bildet eller videoen hvis det finnes. Første
  versjon skal støtte JPEG EXIF, metadata i vanlige MP4/MOV/M4V/3GP-filer og
  RIFF/INFO-dato i AVI-filer. Hvis metadata ikke finnes, må man se på om filens
  endringsdato eller filnavn gir informasjon.
- Hvordan skal programmet rapportere feil, for eksempel utilgjengelige mapper
  eller filer som ikke kan leses? Første utgave av programmet kan skrive om
  dette til stdout og registrere feilen i databasen.

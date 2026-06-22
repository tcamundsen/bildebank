# Bildesorteringsprogram

Filen devel-docs/begrensninger-og-krav.md inneholder ting som vi
ikke skal implementere, og hva vi krever at brukeren skal forstå.

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

## Sikkerhetskrav ved import og unimport

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
Unntak: Google/Pixel motion-videoer med filendelsen `.MP` kan lagres i
målmappen med `.mp4` når filinnholdet faktisk er en MP4-container. Kildemappen
endres fortsatt ikke, og databasen beholder originalfilnavnet med `.MP`.
Ved navnekollisjon i samme måned, så legges "-1", "-2" etc til filnavnet,
før filendelsen, for eksempel `IMG1324-2.jpg`. Samtidig må det markeres
i databasen at dette bildet har fått lagt til "-1" på grunn av navnekollisjon.
Kommando for å liste bilder med navnekollisjon

unimport må være konservativ, verifiser kildefiler før endring, aldri føre til
tap, og fjerne bare proveniens når andre kilder fortsatt peker på samme fil.

## Låsing av samlingsendringer

Operasjoner som flytter filer i bildesamlingen og samtidig oppdaterer
hoveddatabasen, skal holde bildesamlingens target-lås fra før første
databaseoppslag og validering til etter at databaseendringen er committed.
Dette gjelder uavhengig av om operasjonen startes fra kommandolinjen eller
webgrensesnittet.

## Teknologi

Programmet skal skrives i Python. Planen er at dette skal være et program
som utelukkende kjøres fra kommandolinjen. Det er høy prioritet å garantere
at alle unike bilder fra alle kildemapper som importeres blir med i 
målmappen.

Databasen bør være SQLite. SQLite gir transaksjoner, indekser og trygg lokal
lagring uten å kreve en separat databaseserver.

## Databaseversjoner

- gjeldende schema er v11
- historiske migreringer ligger i devel-docs/database-v4-migration.md og
  devel-docs/database-v5-migration.md, devel-docs/database-v6-migration.md og
  devel-docs/database-v7-migration.md, devel-docs/database-v8-migration.md,
  devel-docs/database-v9-migration.md, devel-docs/database-v10-migration.md
  og devel-docs/database-v11-migration.md
- ny runtime-kode skal anta v11, med mindre oppgaven eksplisitt gjelder
  migrering

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

Enhetstester kan kjøres i WSL under utvikling, men før programmet tas i bruk på
den faktiske bildesamlingen må importflyten testes i Windows 11 med ekte
Windows-stier og et lite testsett med bilder og videoer.

Bildebank skal ikke kjøres fra WSL mot en bildesamling som ligger på et
Windows-filsystem. Alle kommandoer som bruker en slik samling skal avvises,
også lesekommandoer og serveren. En bildesamling på WSLs eget Linux-filsystem
kan fortsatt brukes fra WSL.

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

## Browserutvalg i run-server

`run-server` skal behandle bildebrowseren som en felles visning som kan brukes
for mange forskjellige bildeutvalg. Nye utvalg, for eksempel bilder for en
person, bilder fra en kilde, bilder fra et geografisk område eller kombinasjoner
av sted, år og personer, bør derfor gjenbruke den felles browserflyten.

Ny funksjonalitet bør primært beskrive hvilket utvalg som skal vises, og så
bruke felles funksjoner for selve browseren, for eksempel
`source_item_page_html`, `source_month_page_html`, `source_item_url`,
`source_items`, navigasjon mellom bilder og månedsnavigasjon. Det gjør at nye
utvalg får samme blaing, rotering, bildeinfo, sletting og lenkestruktur uten at
det lages egne parallelle browsere for hvert tilfelle.

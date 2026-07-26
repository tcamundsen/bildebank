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
- **Duplikatfunn**: En fil i kilden som ikke kopieres fordi programmet finner en
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
hash på kopien matcher hash på filen i kilden, og deretter gi filen endelig navn.
Filen skal først registreres som importert i databasen etter vellykket kopiering
og verifisering.

Kopieringen skal fungere på vanlige filsystemer som brukes på Windows, eksterne
disker og Linux, for eksempel NTFS, exFAT, FAT32, SMB/nettverksmapper og ext4.
Programmet skal derfor ikke være avhengig av filsystemfunksjoner som ikke er
universelt tilgjengelige, for eksempel hardlinks. Midlertidig fil bør ligge i
samme mappe som den endelige filen slik at endelig rename/flytting skjer innenfor
samme filsystem.

Når bildene (og videoene) importeres, så skal ikke filnavnet deres endres.
Unntak: Google/Pixel motion-videoer med filendelsen `.MP` kan lagres i
målmappen med `.mp4` når filinnholdet faktisk er en MP4-container. Kildemappen
endres fortsatt ikke, og databasen beholder originalfilnavnet med `.MP`.
Ved navnekollisjon i samme måned, så legges "-1", "-2" etc til filnavnet,
før filendelsen, for eksempel `IMG1324-2.jpg`. Samtidig må det markeres
i databasen at dette bildet har fått lagt til "-1" på grunn av navnekollisjon.
Kommando for å liste bilder med navnekollisjon

AVI- og 3GP-originaler skal forbli uendrede canonical-filer i `files`. Nettleseravspilling
kan bruke regenererbare MP4-kopier under `video-previews/v1`, adressert med
originalens SHA-256. Kopiene lages eksplisitt, aldri under import eller en
HTTP-forespørsel, og registreres ikke som nye canonical-filer eller
`file_sources`. En kopi får endelig navn først etter vellykket konvertering og
validering. Regenererbare videokopier utelates fra snapshots; AVI- og 3GP-originaler,
også under `deleted/`, følger de vanlige snapshotreglene.

unimport må være konservativ, verifiser filene i kilden før endring, aldri føre til
tap, og fjerne bare proveniens når andre kilder fortsatt peker på samme fil.
Alle registrerte originalfiler skal kontrolleres på nytt etter at brukeren har
bekreftet operasjonen. Hvis en fil endres mens et bekreftelsesspørsmål venter,
skal `unimport` stoppe uten databaseendringer.
Hvis en fil som skal fjernes ved `unimport` ikke lenger matcher databaseført
størrelse og SHA-256, skal brukeren varsles og eksplisitt bekrefte før
`unimport` fortsetter.

`unimport` er et eksplisitt unntak fra hovedregelen om at kommandoer ikke skal
slette mediefiler permanent. Unntaket gjelder bare etter at alle registrerte
originalfiler i kilden er kontrollert med størrelse og SHA-256. Mediefiler som
ikke har referanser fra andre importer fjernes da fra bildesamlingen, og alle
referanser til importen fjernes. En kølagt sletting skal være bundet til
forventet størrelse og SHA-256 og må nekte å slette hvis innholdet på stien har
endret seg.

## Låsing av samlingsendringer

Operasjoner som flytter filer i bildesamlingen og samtidig oppdaterer
hoveddatabasen, skal holde bildesamlingens target-lås fra før første
databaseoppslag og validering til etter at databaseendringen er committed.
Dette gjelder uavhengig av om operasjonen startes fra kommandolinjen eller
webgrensesnittet.

En intern filflytting skal aldri overskrive en fil som allerede finnes på
målstien, heller ikke hvis målfilen dukker opp etter den første valideringen.
Kilden skal verifiseres mot databaseført SHA-256 før flyttejournalen committes,
og den flyttede filen skal verifiseres før operasjonen markeres fullført.
Ved en uavklart filtilstand skal automatisk recovery ikke fjerne noen av
filstiene som finnes; operasjonen skal stoppe for manuell avklaring.

## Gjenoppretting av en manglende samlingsfil

En databaseført fil som mangler på disk, skal ikke fjernes fra databasen eller
erstattes automatisk. En egen reparasjon kan legge filen tilbake når brukeren
oppgir en kopi utenfor samlingen og kopiens størrelse og SHA-256 er eksakt lik
den konsistente identiteten i `files` og `file_sources`.

Reparasjonen skal ha dry-run som standard og holde target-låsen ved apply.
Kandidaten skal kopieres, aldri flyttes, og den eksterne kopien skal beholdes.
Kandidaten skal først kopieres til en midlertidig fil og verifiseres før
publisering. En eksisterende målsti skal aldri overskrives. Hoveddatabasen
skal ikke endres, og en slettet filrad skal fortsatt peke under `deleted/`.

## Eksterne InsightFace-modeller

Modellfiler er regenererbare, men de tolkes av eksterne biblioteker og må
behandles som nedlastet programinnhold. Automatiske modellnedlastinger skal
derfor bruke en fast URL og SHA-256, trekke ut bare forventede filer og
publisere fra staging etter full kontroll.

En eksisterende, ikke-tom modellmappe som er ufullstendig eller avviker fra den
fastlåste modellen, skal ikke erstattes automatisk. Face-databasen kan inneholde
embeddings laget med akkurat denne modellen. Mappen skal bevares og kreve
manuell avklaring før en annen modell installeres under samme navn.

## Teknologi

Programmet skal skrives i Python. Planen er at dette skal være et program
som utelukkende kjøres fra kommandolinjen. Det er høy prioritet å garantere
at alle unike bilder fra alle kildemapper som importeres blir med i 
målmappen.

Databasen bør være SQLite. SQLite gir transaksjoner, indekser og trygg lokal
lagring uten å kreve en separat databaseserver.

## Databaseversjoner

- gjeldende schema er v18
- eldste hoveddatabaseformat som støttes av gjeldende migrator er v5
- v1–v4 er historiske, utfasete formater; eldre hjelpegrener i koden er ikke
  et løfte om at disse formatene kan oppgraderes direkte
- en uventet v1–v4-database skal bevares uendret og håndteres etter
  gjenopprettingsveien i devel-docs/database-v5-migration.md
- historiske migreringer ligger i devel-docs/database-v4-migration.md og
  devel-docs/database-v5-migration.md, devel-docs/database-v6-migration.md og
  devel-docs/database-v7-migration.md, devel-docs/database-v8-migration.md,
  devel-docs/database-v9-migration.md, devel-docs/database-v10-migration.md
  devel-docs/database-v11-migration.md,
  devel-docs/database-v12-migration.md og
  devel-docs/database-v13-migration.md og
  devel-docs/database-v14-migration.md,
  devel-docs/database-v15-migration.md og
  devel-docs/database-v16-migration.md,
  devel-docs/database-v17-migration.md og
  devel-docs/database-v18-migration.md
- ny runtime-kode skal anta v18, med mindre oppgaven eksplisitt gjelder
  migrering
- den separate OpenCLIP-databasen har schema v1 og er beskrevet i
  devel-docs/openclip-database.md

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
importert, slik at man kan gå fra en liten testimport til en større import. Den
tidligere undermappen forblir en vanlig kilde. Filer som finnes i begge importer
får flere `file_sources`-rader.

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

Når programmet finner et eksakt duplikat, skal filen i kilden ikke kopieres på nytt.
Databasen skal likevel registrere duplikatfunnet med original kildepath og
hvilken fil i målmappen den matcher. På den måten kan brukeren senere se at
filen faktisk ble funnet og vurdert.

Sletting med `remove` er en beslutning om at filen ikke skal være aktiv i
bildesamlingen. En slettet `files`-rad som peker til `deleted/`, skal derfor
fortsatt delta i SHA-256-basert duplikatgjenkjenning. Hvis en senere import
finner samme filinnhold, skal importen registrere ny `file_sources`-rad mot den
slettede `files`-raden, ikke kopiere inn en ny aktiv fil og ikke automatisk
gjenopprette bildet.

Samme beslutning betyr at data som gjør bildet søkbart eller bruker det i
ansiktsgjenkjenning, skal fjernes. `remove` skal slette bildets OpenCLIP-rader
og bildeavhengige rader fra alle InsightFace-modelldatabaser. Det omfatter
bekreftede ansiktskoblinger, manuelle person-fil-koblinger og forslag som
bruker et av bildets ansikter som referanse. Globale `persons`-rader beholdes.
`undelete` gjenoppretter filen og hoveddatabasens metadata, men ikke disse
sidecar-dataene.

For et aktivt bilde skal sidecar-dataene beholdes så lenge minst én
`file_sources`-rad finnes. `unimport` skal derfor bare slette sidecar-data når
den aktuelle unimporten fjerner den siste kildereferansen og dermed selve
`files`-raden. Sidecar-opprydding og hoveddatabaseendring skal skje i samme
transaksjon.

Dette endrer ikke kontrakten for selve mediefilen: `remove` beholder den under
`deleted/`. En eventuell fremtidig funksjon for permanent tømming av
`deleted/` er en egen destruktiv operasjon som også må avklare proveniens,
senere reimport og eksisterende snapshots.

Før importen hopper over kopiering på grunn av et database-treff på SHA-256, må
den verifisere at den registrerte filen fortsatt finnes på disk og har
forventet SHA-256. Hvis filen mangler eller innholdet ikke matcher databasen, er
det en integritetsfeil for den aktuelle filen i kilden. Importjobben skal registrere
feilen og fortsette med andre filer, uten å reparere, overskrive eller
gjenopprette automatisk.

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

I read-only-modus skal også direkte medieendepunkter bare gi tilgang til
aktive `files`-rader. At innstillinger og siden for fjernede filer er skjult,
er ikke i seg selv en tilstrekkelig tilgangsgrense. Før serveren åpner en
databaseført original, previewkilde eller thumbnail, skal den validere
samlingsstien og filtypen uten å følge symlinker eller Windows reparse points.
Filen skal åpnes kontrollert før HTTP-responsen starter, slik at en fil som
byttes etter stioppslaget ikke kan omgå kontrollen.

Serverens POST-kall er små tekstbaserte skjema- eller JSON-kall, ikke
filopplasting. Request body skal derfor ha en fast maksimumsgrense på 1 MiB,
én entydig og gyldig `Content-Length` og gyldig UTF-8.
`Transfer-Encoding` støttes ikke. Ugyldig framing skal avvises før CSRF- og
endepunktbehandling, og forbindelsen skal lukkes uten at en for stor body
leses.

Read-only-serveren skal heller ikke endre tilstand under oppstart. Den skal
lese config uten legacy-migrering, ikke registrere samlingen som sist brukt i
den lokale programdatabasen og åpne hoveddatabasen read-only for å kontrollere
gjeldende schema. Dette gjelder også `--lan-share` og `--slideshow`. Vanlig
skrivbar server beholder den ordinære databaseforberedelsen.

En skrivbar server på en adresse som kan nås fra andre maskiner, skal kreve
`--allow-remote-write` i tillegg til `--allow-remote`. Kontrollen skal skje før
recovery og databaseforberedelse. Ekstern read-only, LAN-share, slideshow og
lokal skrivbar server krever ikke dette ekstra flagget. Flagget er en
uttrykkelig risikobekreftelse, ikke autentisering; alle klienter som kan nå en
skrivbar server, kan hente CSRF-token og utføre tillatte endringer.
Wildcard-adressene `0.0.0.0` og `::` er bare lytteadresser. Klar-meldingen og
automatisk nettleseråpning skal bruke henholdsvis `127.0.0.1` og `[::1]`
lokalt, uten å endre hvilken adresse serveren lytter på.

`--lan-share` er en egen presentasjonsprofil i tillegg til å være read-only.
Den deler aktive originalfiler, nøyaktig GPS, kommentarer, personer og tagger,
men skal ikke vise maskinspesifikke `sources.path`,
`file_sources.source_path` eller lokale snapshot-repositorystier. Kildenavn og
snapshotstatus kan vises. Denne redigeringen gjelder den uttrykkelige
LAN-share-profilen; vanlig lokal read-only og en manuelt sammensatt
`--host`/`--allow-remote`-modus beholder den ordinære detaljvisningen.
Slideshow beholder sin strengere, separate rutegrense.
LAN-share og slideshow skal heller ikke sende rå exception-tekster til
klienten, fordi feil fra filsystem, SQLite og sidecar-biblioteker kan inneholde
maskinspesifikke stier. De skal vise en kort feil som beskriver operasjonen.
Vanlig lokal server kan fortsatt vise den detaljerte exception-teksten.

Alle ekte HTTP-forespørsler skal valideres før GET-/POST-ruting. De skal ha
nøyaktig én gyldig `Host`-header. Hvis `Host` oppgir en port, skal den være
serverens faktiske port. Ved binding til en bestemt adresse godtas den
konfigurerte adressen, den faktiske bundne adressen og `localhost`. Ved
wildcard-binding godtas IP-litteraler og `localhost`, men ikke vilkårlige
domenenavn; et ønsket vertsnavn må bindes eksplisitt. Dette hindrer at et
angriperstyrt DNS-navn brukes til rebinding mot den lokale serveren. Hvis en
`Origin`-header finnes, skal den være en enkel `http`-origin med samme
vertsnavn/IP som `Host` og serverens faktiske port. Manglende `Origin` forblir
tillatt for vanlig navigasjon og enkle klienter. Muterende POST-kall krever i
tillegg fortsatt gyldig CSRF-token.

Alle ferdige HTTP-responser, også redirects, direkte medier og tidlige
feilresponser, skal ha `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin` og `X-Frame-Options: DENY`. `same-origin` skal
beholde en presis `Origin` på Bildebanks egne POST-skjemaer, samtidig som
Bildebank-adressen ikke sendes som referrer til eksterne nettsteder. Bildebank
har ingen dokumentert iframe-integrasjon, og serveren bruker ikke `Referer` som
del av funksjonaliteten. `Server`-headeren skal identifisere Bildebank uten å
røpe Python-versjonen. CSP skal ikke legges til som del av denne pakken; inline
HTML, CSS og JavaScript må kartlegges og tilpasses før en bred CSP innføres.

Dashboard- og maintenance-status er rene statusoppslag, også når den vanlige
serveren er skrivbar. De skal åpne hoveddatabasen og eksisterende OpenCLIP- og
InsightFace-databaser read-only. Dashboardet skal ikke opprette eller migrere
tabeller i den lokale programdatabasen. Et statusoppslag skal heller ikke
adoptere eller migrere et eldre sidecar-schema eller oppdatere sidecarens
metadata.

Vanlige person-, item- og filtersider skal heller ikke bruke GET til å
opprette eller migrere InsightFace-databasen. Serverens face-lesehjelpere skal
kreve gjeldende eksplisitt schema og åpne den eksisterende databasen
read-only. En personfiltrering uten face-database kan feile uten å opprette en
tom database. Migrering hører til en eksplisitt face-operasjon i skrivbar
modus.

Browserqueries og sidecaroppslag som kan vedlegge face-databasen, skal åpne
hoveddatabasen gjennom den felles read-only-tilkoblingen. Dette håndhever
`query_only` og aktiverer SQLite URI-støtte som kreves for at et vedlegg med
`mode=ro` skal fungere likt på Windows og Linux. Serveroppstarten skal gjøre
den komplette schemasjekken én gang. Senere read-only-tilkoblinger i samme
prosess skal fortsatt kontrollere `schema_version`, men skal ikke gjenta hele
strukturkontrollen for hver browserforespørsel.

OpenCLIP-søk laster en kostbar modell og registrerer søkekjøring og resultater
i OpenCLIP-databasen. Selve søket og eksplisitt forhåndslasting av modellen
skal derfor bare kunne startes med CSRF-beskyttet POST. GET `/search` skal
bare vise søkeskjemaet, også når en gammel URL inneholder søkeparametre.
Eksplisitt telling av thumbnails skal av samme grunn bruke beskyttet POST.
Automatisk vedlikeholdsstatus kan forbli GET fordi den bare gjør read-only
databaseoppslag og også skal fungere i read-only-modus.

## Lokal status for snapshots

Et publisert snapshot kan registreres i programmets lokale programdatabase
etter at snapshotoperasjonen er fullført. Registreringen inneholder bare
opplysninger som launcher og dashboard trenger: collection-ID, repository-ID,
sist brukte repository-sti, snapshot-ID, status og tidspunkt.

Denne hjelpetilstanden er ikke en del av repositoryformatet eller
bildesamlingens database. En feil i lokal registrering skal derfor aldri gjøre
et allerede publisert snapshot til en feil. Snapshotkjernens publisering,
låsing, manifest og objektlagring skal ikke endres for å vedlikeholde lokal
status.

Repository-ID, ikke Windows-sti eller stasjonsbokstav, identifiserer et
repository. Flere USB-disker kan dermed bruke samme sti når de kobles til etter
tur. Klonede repositories må ikke brukes videre som uavhengige, skrivbare
repositories, fordi klonen beholder originalens repository-ID.

## Snapshotfunksjoner i launcheren

Oppretting, kontroll og gjenoppretting av enkeltfiler samles på en egen
snapshotfane. Fanen viser sist kjente, tilgjengelige repository for den valgte
samlingen, men brukeren kan velge et annet repository. Kontroll og
enkeltfilgjenoppretting skal fungere selv om den aktive bildesamlingen mangler.

Ved enkeltfilgjenoppretting velger brukeren først et publisert snapshot og
blar deretter i år, måned og filnavn. `udatert/` og `deleted/` vises som egne
grener. Filen eksporteres til en mappe utenfor bildesamlingen gjennom den
eksisterende restoreplanen. Launcheren skal aldri overskrive en fil, endre
repositoryet eller legge eksporten automatisk tilbake i bildesamlingen.

## Kommentarer på mediefiler

En mediefil kan ha én kommentar på den kanoniske `files`-raden. Kommentaren
tilhører ikke en bestemt importkilde og lagres aldri i metadata i selve
bildefilen. Den bevares når filen flyttes til `deleted/` og tilbake.

Kommentarer vises bare i full item-visning, slideshow og statiske browsere,
ikke på oversiktsbilder eller søkeresultat-miniatyrer. Redigering skjer bare i
skrivbar servermodus og holder target-låsen gjennom databaseoppdateringen.

# Plan for versjonert backup

Status: **første diskusjonsutkast – ikke godkjent for implementering**

Dette dokumentet skisserer en ekte, versjonert backup for Bildebank. Hensikten
er å bli enige om sikkerhetsmodell, lagringsformat, kommandoer og
gjenoppretting før det skrives produksjonskode.

Eksisterende `bildebank backup` er en speiling. Den er fortsatt nyttig som en
oppdatert reservekopi, men den sletter filer fra backupen når de ikke lenger
finnes i samlingen. Den gir derfor ikke historiske versjoner og beskytter ikke
alene mot at en feil eller utilsiktet sletting blir speilet videre.

## Mål

Den nye løsningen skal:

- bevare flere komplette tilstander av en bildesamling
- aldri overskrive eller fjerne innhold som et fullført snapshot trenger
- bruke eksisterende SHA-256-data til å kontrollere databaseførte mediefiler
- ta med aktive filer, `deleted/`, hoveddatabasen og andre nødvendige databaser
- kunne fortsette trygt etter avbrudd, full disk eller strømbrudd
- kunne oppdage manglende og korrupte backupobjekter
- ha eksplisitt og konservativ gjenoppretting av hele samlinger og enkeltfiler
- fungere på Windows 11 og vanlige eksterne filsystemer uten hardlinks
- være forståelig nok til at backupen kan reddes manuelt hvis programmet en
  dag ikke lenger finnes

Et snapshot skal beskrive hvordan samlingen så ut på ett bestemt tidspunkt.
Ny backup skal legge til et nytt snapshot, ikke gjøre et gammelt snapshot likt
dagens samling.

## Ikke mål i første versjon

Første versjon skal ikke:

- automatisk slette gamle snapshots eller backupobjekter
- ha en `prune`- eller garbage collection-kommando som permanent sletter
  bildefiler
- automatisk reparere den aktive bildesamlingen
- skrive tilbake til eller endre opprinnelige kildemapper
- følge symbolske lenker, junctions eller andre lenker ut av samlingen
- love beskyttelse mot ransomware når backupmediet står tilkoblet og skrivbart
- erstatte behovet for flere backupmedier, frakoblet kopi og kopi utenfor
  boligen

Komprimering, kryptering og støtte for skylagring avgjøres senere. Disse
egenskapene må ikke forsinke en enkel og kontrollerbar lokal første versjon.

## Forholdet mellom `files` og `file_sources`

I databaseschema v14 er `files` den kanoniske raden for en fysisk fil i
bildesamlingen. Den inneholder blant annet:

- `target_path` og `target_path_key`
- `sha256` og `size_bytes`
- `deleted_at` og `deleted_original_target_path`

`file_sources` beskriver hvor filinnholdet ble funnet ved import. Hver rad
peker til `files.id` gjennom `file_id` og til `sources.id` gjennom `source_id`.
Flere `file_sources`-rader kan derfor peke til samme `files`-rad når samme
filinnhold finnes i flere kilder.

Backupen skal ikke bruke `file_sources.source_path` som sted å hente
backupinnhold fra. Kildemappene kan være frakoblet eller ikke lenger finnes.
Filinnholdet skal leses fra den aktive samlingen via `files.target_path`.
Provenienshistorikken i `file_sources` bevares ved at hoveddatabasen inngår i
hvert snapshot.

## Foreslått sikkerhetsmodell

Første versjon bør bruke et append-only, innholdsadressert repository:

1. Hvert unikt filinnhold lagres som et objekt identifisert med SHA-256 og
   størrelse.
2. Et snapshotmanifest kobler relative stier og metadata til objektene som
   fantes da snapshotet ble tatt.
3. Et objekt publiseres først etter kopiering og kontroll av SHA-256.
4. Et snapshot publiseres først etter at alle objekter og databasekopier er
   ferdige og verifiserte.
5. Et publisert snapshot endres aldri.
6. En avbrutt kjøring skal ikke kunne gjøre et tidligere snapshot ugyldig.

SHA-256 beskytter mot tilfeldig korrupsjon og feilkopiering, men er ikke alene
bevis på at en angriper ikke har endret både objekter og manifest. Beskyttelse
mot en angriper krever i praksis frakoblet eller skrivebeskyttet media, eller
senere støtte for signerte manifester med separat nøkkel.

## Foreløpig repository-format

Dette er et forslag som må vurderes før formatet fryses:

```text
backup-repository/
  .bildebank-backup-repository.json
  objects/
    sha256/
      ab/
        cd/
          abcdef...                                 
  snapshots/
    <collection-id>/
      2026-07-15T183045Z-<snapshot-id>/
        manifest.json
  incomplete/
    <run-id>/
  tmp/
```

Repositorymetadata skal minst ha:

- stabil `repository_id`
- `format_version`
- `created_by` og Bildebank-versjon
- opprettelsestidspunkt

Snapshotmanifestet skal minst ha:

- `snapshot_id`, `collection_id` og repository-ID
- start- og sluttidspunkt
- Bildebank-versjon og schema-versjoner
- liste over alle inkluderte relative stier
- objekthash, størrelse og filtype for hver sti
- hvilke filer som er laget med SQLite backup-API
- antall filer og byte, samt en hash av det kanoniske manifestinnholdet
- eksplisitte eksklusjoner og eventuelle advarsler

`complete` bør uttrykkes ved at det endelige manifestet publiseres atomisk som
siste steg. En separat, overskrivbar statusfil skal ikke være nødvendig for å
avgjøre om et snapshot er gyldig.

Et globalt repositoryregister kan brukes som hurtigbuffer, men snapshots skal
kunne oppdages og gjenopprettes uten at et slikt register er intakt.

### Objektnavn og kollisjoner

Objektnøkkelen bør bestå av algoritme, SHA-256 og størrelse. Hvis et objekt med
samme nøkkel allerede finnes, skal det kontrolleres før det gjenbrukes. Et
eksisterende objekt med feil størrelse eller feil hash er repositorykorrupsjon;
det skal aldri overskrives automatisk.

Kopiering skal gå til en unik midlertidig fil på samme filsystem. Etter
verifisering får objektet endelig navn med en atomisk rename. Implementasjonen
skal ikke være avhengig av hardlinks, reflinks eller filsystem-snapshots.

## Hva et snapshot skal inneholde

Foreløpig anbefaling er å inventere hele samlingsmappen, ikke bare radene i
`files`. Det gir mulighet til å gjenopprette samlingen selv om det finnes en
ukjent fil eller databasen er ufullstendig.

Snapshotet skal inkludere:

- alle aktive bilder og videoer
- alt under `deleted/`
- `.bilder.sqlite3`
- andre Bildebank-databaser som ligger i samlingen, blant annet databaser for
  søk og ansiktsmodeller
- nødvendige konfigurasjons- og metadatafiler i samlingen
- andre vanlige filer med mindre de er eksplisitt klassifisert som
  regenererbare eller runtime-filer

Kjente runtime-filer som target-lås og aktiv logg skal ikke tas med. Det må
lages én eksplisitt og testet liste over eksklusjoner. Ukjente filer skal som
hovedregel tas med og rapporteres, ikke ignoreres.

Det må avgjøres om generert HTML og thumbnails skal tas med. Det sikreste er å
ta dem med, mens det mest plassbesparende er å klassifisere dem som
regenererbare. Dette er en åpen beslutning.

Symbolske lenker, junctions og reparse points skal ikke følges. De skal
rapporteres tydelig, og snapshotet skal som utgangspunkt ikke markeres komplett
før policyen for det konkrete tilfellet er avklart.

## Integritetskontroll mot hoveddatabasen

For hver `files`-rad skal backupen finne den forventede filen under samlingen,
og kontrollere:

- at stien er innenfor samlingsmappen
- at stien er en vanlig fil
- at størrelsen er lik `files.size_bytes`
- at SHA-256 er lik `files.sha256`

Dette gjelder både aktive og slettede rader. Hvis filen mangler eller har annet
innhold, skal backupen ikke betrakte det endrede innholdet som en ny, gyldig
utgave av den databaseførte filen. Kjøringen skal rapportere integritetsfeilen
og ikke publisere et komplett snapshot.

Objekter som allerede er kopiert i samme mislykkede kjøring kan ligge igjen
som urefererte, verifiserte objekter. Det er tryggere enn å slette mulig
bildefilinnhold automatisk. En senere backup kan gjenbruke dem etter ny
verifisering.

Vanlige filer som finnes på disk, men ikke er databaseført, bør tas med med
nyberegnet SHA-256 og få en tydelig advarsel i rapporten. Det følger prinsippet
om at det er bedre å sikre én fil for mye enn én for lite.

## Konsistent snapshot av databaser og filer

Reell snapshot-oppretting skal holde samlingens `TargetLock` fra før første
databaseoppslag og filinventar til snapshotmanifestet er publisert eller
kjøringen har feilet. Det viderefører sikkerhetsmodellen til dagens backup og
hindrer andre Bildebank-kommandoer i å endre samlingen underveis.

SQLite-databaser skal ikke kopieres som vanlige åpne filer. Det skal opprettes
en konsistent kopi gjennom SQLite backup-API til et stagingområde. Kopien skal
integritetskontrolleres og deretter lagres som et vanlig backupobjekt.

Det finnes ingen felles transaksjon på tvers av alle databasefilene. Target-
låsen skal derfor hindre Bildebank fra å skrive til noen av dem mens snapshotet
bygges. Snapshotet publiseres først når alle databasekopiene og filobjektene er
ferdige.

Eksterne programmer kan fortsatt endre en bildefil uten å respektere låsen.
Backupen må derfor kontrollere størrelse og SHA-256 etter lesing. Hvis filen
endres under kopiering, skal den aktuelle kjøringen feile konservativt.

## Foreslått kjøresekvens

1. Valider aktiv samling, repository og `collection_id`.
2. Kontroller at repository ikke ligger i eller over samlingsmappen.
3. Ta `TargetLock`.
4. Opprett unik `run-id` og stagingområde.
5. Inventer samlingsmappen uten å følge lenker.
6. Les `files` og valider databaseførte stier, størrelser og SHA-256.
7. Lag konsistente kopier av alle SQLite-databaser med SQLite backup-API.
8. Kopier og verifiser objekter som ikke allerede finnes gyldig i repository.
9. Bygg et deterministisk manifest og kontroller alle referanser.
10. Publiser snapshotet atomisk.
11. Skriv sluttrapport og frigjør låsen.

Ved feil skal ingen tidligere snapshots eller objekter endres. En ny kjøring
skal kunne gjenbruke ferdig verifiserte objekter og ellers starte en ny,
uavhengig stagingkjøring.

## Kommandoer og overgang fra dagens mirror

Dagens `bildebank backup` har etablert betydning og eksisterende backupformat.
Det er farlig å tolke en gammel mirror-mappe som et nytt repository eller å
endre oppførselen lydløst.

Foreløpige alternativer:

1. Behold `bildebank backup` som mirror og innfør `bildebank snapshot` for ny
   løsning.
2. Innfør underkommandoer som `bildebank backup mirror` og
   `bildebank backup create`, med en tydelig overgang for dagens syntaks.
3. Gjør `bildebank backup` til versjonert backup i en senere hovedversjon og
   flytt dagens funksjon til `bildebank mirror`.

Foreløpig anbefaling er alternativ 1 i første implementasjon. Det gir minst
risiko mens format og arbeidsflyt prøves ut. Navnet `snapshot` må likevel testes
mot målgruppen; `backup-version` eller en annen tydelig formulering kan være
lettere å forstå.

Et mulig første kommandosett er:

```text
bildebank snapshot create PLASSERING [--dry-run]
bildebank snapshot list PLASSERING
bildebank snapshot check PLASSERING [--full]
bildebank snapshot restore PLASSERING SNAPSHOT-ID NY-MAPPE [--dry-run]
bildebank snapshot restore-file PLASSERING SNAPSHOT-ID FILSTI MÅLMAPPE [--dry-run]
```

Alle skrivende restore-operasjoner skal ha dry-run. Hel gjenoppretting skal som
standard kreve en ny eller tom målmappe og aldri skrive over en eksisterende
bildesamling.

## Gjenoppretting

En backup er ikke ferdig designet før restore er spesifisert og testet.

Hel restore skal:

- validere repositorymetadata, snapshotmanifest og alle nødvendige objekter
- vise valgt samling, snapshotdato, antall filer og plassbehov
- avvise eksisterende ikke-tom målmappe som standard
- kopiere via midlertidige filer og verifisere SHA-256 etter kopiering
- gjenopprette opprinnelige relative stier, inkludert `deleted/`
- gjenopprette databasekopiene som vanlige SQLite-filer
- ikke kopiere repositorymetadata inn i den gjenopprettede samlingen
- kjøre database- og filintegritetskontroll før samlingen tas i bruk
- skrive en tydelig rapport, men ikke automatisk reparere avvik

Restore av enkeltfil skal kreve eksplisitt snapshot og filsti. Standardmålet
bør være en vanlig eksportmappe utenfor aktiv samling. Restore direkte inn i
aktiv samling bør ikke være med i første versjon, fordi det krever koordinert
oppdatering av både filsystem og database.

## Kontroll av backupen

Det bør finnes to kontrollnivåer:

- En rask kontroll validerer repositorystruktur, manifester, objektnavn,
  størrelser og at alle refererte objekter finnes.
- En full kontroll leser alle refererte objekter og beregner SHA-256 på nytt.

Full kontroll kan ta lang tid og må vise fremdrift. Resultatet skal skille
mellom:

- komplett og verifisert snapshot
- manglende objekt
- objekt med feil størrelse eller hash
- ugyldig eller uleselig manifest
- ufullstendig kjøring som aldri ble publisert
- ureferert objekt som ikke gjør eksisterende snapshots ugyldige

Kontrollen skal være read-only. Automatisk sletting eller reparasjon hører ikke
til denne kommandoen.

## Kapasitet og ytelse

Første snapshot må lese og hashe hele samlingen. Senere snapshots kan unngå å
kopiere objekter som allerede finnes, men databaseførte mediefiler bør fortsatt
verifiseres konservativt før snapshotet publiseres.

Det må måles om full SHA-256 av hele samlingen ved hver kjøring er praktisk.
Mulige senere optimaliseringer er kontrollert caching basert på størrelse,
mtime og filidentitet, kombinert med periodisk full kontroll. En slik cache må
aldri gjøre at en fil med uventet innhold godtas uten tilstrekkelig kontroll.

Repositoryet bør kontrollere ledig plass før kopiering, men beregningen blir et
estimat fordi komprimering, samtidige endringer og filsystemoverhead kan
variere. Full disk skal gi en ufullstendig kjøring uten å skade eldre
snapshots.

## Trusselmodell og begrensninger

Løsningen skal beskytte mot:

- utilsiktet sletting som senere oppdages
- feil bruk av `remove`, `unimport` eller fremtidige samlingskommandoer
- korrupsjon eller manglende filer i den aktive samlingen, når en eldre gyldig
  versjon finnes
- avbrutt backup, programkrasj og full backupdisk
- tilfeldig bitråte som oppdages av periodisk full kontroll

Løsningen beskytter ikke alene mot:

- tyveri eller brann når samling og eneste backup er på samme sted
- ransomware eller angriper med skrivetilgang til repositoryet
- tap av både samling og alle backupmedier
- skade som allerede finnes i alle snapshots før den blir oppdaget
- feil i Bildebank-kode som ikke fanges av formatkontroll og tester

Anbefalt drift må derfor fortsatt beskrive flere medier, minst ett frakoblet
medium, ulikt oppdateringstidspunkt og minst én kopi utenfor boligen.

## Tester som kreves før løsningen kan tas i bruk

Minstekrav til automatiserte tester:

- første snapshot og nytt snapshot uten endringer
- ny, flyttet og `remove`-markert fil
- fil under `deleted/`
- flere `file_sources` for samme `files`-rad
- to stier med identisk innhold og bare ett lagret objekt
- manglende databaseført fil
- feil størrelse og feil SHA-256 i aktiv samling
- eksisterende backupobjekt med feil innhold
- ukjent mediafil som ikke finnes i `files`
- konsistent SQLite-kopi, også med WAL/journal i bruk
- avbrudd under objektkopiering og før manifestpublisering
- full disk og andre skrivefeil
- ugyldig repository-ID eller feil `collection_id`
- symlink, junction/reparse point og sti som forsøker å gå ut av samlingen
- Unicode, mellomrom, lange stier og store/små bokstaver på Windows
- rask og full `check`
- hel restore og restore av enkeltfil
- restore til eksisterende eller for liten målmappe
- kontroll av at gamle snapshots fortsatt kan gjenopprettes etter nye kjøringer

Før bruk på den virkelige samlingen må det gjennomføres en Windows-test med et
lite, representativt datasett på samme type backupmedium som skal brukes.
Testen skal omfatte avbrudd og faktisk gjenoppretting, ikke bare oppretting av
backup.

## Foreslåtte implementasjonstrinn

Ingen av trinnene skal startes før de åpne beslutningene nedenfor er behandlet.

### Trinn 0 – Enighet om design

- Avgjør kommandonavn og forholdet til eksisterende mirror.
- Avgjør om formatet skal implementeres av Bildebank eller gjennom et etablert
  backupverktøy.
- Frys første versjon av repository- og manifestformatet.
- Avgjør hvilke regenererbare filer som eventuelt kan utelates.
- Beskriv restorekontrakten fullstendig.

### Trinn 1 – Read-only plan og inventar

- Implementer repositoryvalidering og `--dry-run`.
- Lag filinventar og sammenligning mot `files`.
- Rapporter nødvendig plass, ukjente filer og integritetsavvik.
- Ikke skriv backupdata i dette trinnet.

### Trinn 2 – Objekter og atomisk snapshot

- Implementer staging, verifisert objektkopiering og manifest.
- Implementer konsistent SQLite-backup.
- Test alle avbruddsgrenser og at eldre snapshots aldri endres.

### Trinn 3 – Kontroll

- Implementer snapshotliste, rask kontroll og full SHA-256-kontroll.
- Gjør kontrollen uavhengig av aktiv bildesamling.

### Trinn 4 – Gjenoppretting

- Implementer dry-run og hel restore til tom mappe.
- Implementer restore av enkeltfil til eksportmappe.
- Test faktisk bruk av en gjenopprettet samling.

### Trinn 5 – Dokumentasjon og Windows-pilot

- Skriv brukerdokumentasjon med Windows-stier og uten krav om teknisk
  forkunnskap.
- Kjør pilot mot lite testsett på Windows 11 og eksternt medium.
- Dokumenter verifisering, diskrotasjon og øvelse på restore.

## Åpne beslutninger

Disse punktene bør behandles eksplisitt i neste iterasjoner:

1. Skal Bildebank eie repositoryformatet, eller bruke et etablert versjonert
   backupverktøy som motor bak et enklere Bildebank-grensesnitt?
2. Skal dagens mirror fortsatt hete `backup`, eller skal navnet etter hvert
   reserveres for den versjonerte løsningen?
3. Er `snapshot` et forståelig kommandonavn for målgruppen?
4. Skal generert HTML, thumbnails og andre regenererbare data inngå?
5. Skal alle vanlige filer i samlingsmappen tas med, eller bare en eksplisitt
   tillatt liste?
6. Skal én integritetsfeil hindre hele snapshotet i å bli publisert, eller kan
   et snapshot publiseres som `incomplete` uten å være godkjent for full
   restore?
7. Er full SHA-256-kontroll ved hver kjøring praktisk, eller trengs en trygg
   inkrementell kontrollmodell?
8. Skal manifest være JSON, JSON Lines, SQLite eller en kombinasjon?
9. Trengs komprimering i første versjon, særlig for databaser og genererte
   filer?
10. Trengs kryptering i første versjon, og hvordan skal nøkkeltap håndteres?
11. Skal ett repository kunne inneholde flere `collection_id`-er?
12. Hvordan skal repositoryet håndtere en manuell kopi av en samling som har
    samme `collection_id` som originalen?
13. Skal urefererte, verifiserte objekter beholdes for alltid, eller bare
    rapporteres slik at brukeren kan flytte hele repositoryet til større disk?
14. Hvilket minste sett med metadata må være lesbart uten Bildebank-programmet?

## Foreløpig anbefaling

Inntil punktene over er avgjort, er anbefalt retning:

- separat kommando og separat repositoryformat fra dagens mirror
- append-only, innholdsadressert lagring uten automatisk sletting
- hele samlingsmappen som utgangspunkt, med svært få eksplisitte eksklusjoner
- SHA-256-verifisering mot `files` for både aktive og slettede filer
- SQLite backup-API og target-lås for konsistente snapshots
- atomisk publisert manifest som eneste definisjon av komplett snapshot
- read-only kontroll før restorefunksjonen regnes som ferdig
- restore til ny mappe, aldri automatisk overskriving av aktiv samling

Dette er et diskusjonsgrunnlag. Status skal ikke endres til godkjent før åpne
beslutninger er gjennomgått og de valgte kompromissene er skrevet inn i
dokumentet.

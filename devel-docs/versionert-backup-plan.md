# Plan for versjonert backup

Status: **godkjent implementeringsplan – under implementering**

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

## Avklarte hovedvalg

- Bildebank skal eie og implementere repositoryformatet. Første versjon skal
  ikke bruke et eksternt versjonert backupverktøy som lagringsmotor.
- Alle vanlige filer i samlingsmappen skal tas med, også filer som ikke finnes
  i hoveddatabasen. Bare den eksplisitte eksklusjonslisten skal utelates.
- Hver snapshotkjøring skal lese og beregne SHA-256 for alle databaseførte
  bilder og videoer, også når filstørrelse og endringstid er uendret.
- Gjenbrukte backupobjekter skal ved vanlig snapshot kontrolleres som vanlige
  filer med forventet størrelse, men ikke hashes på nytt. Nye objekter skal
  alltid hashverifiseres etter kopiering. `snapshot check --full` skal brukes
  til periodisk SHA-256-kontroll av hele repositoryet.
- Ett repository skal tilhøre nøyaktig én bildesamling og bindes til samlingens
  `collection_id`. Flere samlinger skal ikke dele repository.
- Repositoryet skal huske sist brukte maskin og samlingssti. En endring skal
  stoppe snapshotet til brukeren eksplisitt bekrefter at samme logiske samling
  er flyttet, ikke kopiert til en ny uavhengig samling.
- Genererte HTML-filer, thumbnails, target-lås og aktiv logg skal stå på den
  eksplisitte eksklusjonslisten.
- Andre regenererbare filer og databaser skal tas med så lenge de ikke senere
  legges uttrykkelig til på eksklusjonslisten.
- Alle objekter skal lagres ukomprimert i første formatversjon. Komprimering
  skal ikke være en valgfri kodevei i første versjon.
- Bildebank skal ikke kryptere repositoryet i første formatversjon. Eventuell
  kryptering av backupmediet håndteres utenfor Bildebank.
- Verifiserte objekter som ikke refereres av et publisert snapshot, skal
  beholdes permanent, kunne gjenbrukes og aldri slettes av en cleanup- eller
  prune-kommando.
- Hvert snapshot skal ha `manifest.json` for overordnet metadata og
  `files.jsonl` med én filpost per linje, samt en liten `commit.json` som
  kontrollerer begge filene. De skal være tilstrekkelige for kontroll og
  restore uten en repositorydatabase.
- Repositorymetadata og hvert snapshotmanifest skal ha `format_version: 1`.
  Publiserte snapshots skal aldri migreres eller skrives om. En skriver skal
  avvise et repository med en formatversjon eller påkrevd egenskap den ikke
  forstår.
- Repositoryet skal være teknisk manuelt gjenopprettbart, men trenger ikke
  kunne blaes som en vanlig bildemappe. Råobjekter og tekstmanifest skal være
  nok til å finne, kopiere og gi en fil tilbake riktig navn.
- Første implementasjon skal bruke `bildebank snapshot` for den versjonerte
  løsningen. Dagens `bildebank backup` skal fortsatt være mirror med uendret
  betydning. Eventuell senere navneendring avgjøres etter brukertesting.
- `PLASSERING` i snapshotkommandoene skal være den eksakte repositorymappen,
  ikke en foreldremappe der Bildebank utleder navn fra samlingen.
- En manglende eller helt tom repositorymappe kan initialiseres. En ikke-tom
  mappe uten gyldig repositorymetadata skal avvises urørt, og første versjon
  skal ikke ha `--adopt`.
- Integritetsavvik i én fil skal ikke hindre at resten av samlingen får et
  gjenopprettbart snapshot. Snapshotet og den aktuelle filposten skal
  merkes som beskrevet under integritetskontroll.
- Bekreftet feil i hoveddatabasen skal gi et publisert `recovery`-snapshot når
  et allerede initialisert repository er bundet til sist bekreftede maskin og
  samlingssti. Snapshotet skal bevare alt lesbart innhold og rå databasefiler,
  men skal ikke presenteres som en normalt gjenopprettbar bildesamling.
- Recovery etter feil i hoveddatabasen skal bare tillates mot et allerede
  initialisert repository på sist bekreftede maskin og samlingssti. En UUID som
  kan leses fra den skadede databasen er uverifisert og skal aldri initialisere
  eller binde et nytt repository.
- Bekreftet feil i en tilleggsdatabase, som OpenCLIP- eller ansiktsdatabase,
  skal gi `degraded`, ikke `recovery`. Rå databasefiler skal bevares for
  undersøkelse mens den gyldige hoveddatabasen sikres normalt.
- Ved hel restore av et `degraded` snapshot skal forventet, databaseført
  variant gjenopprettes til ordinær plass når den finnes. En avvikende,
  observert variant skal eksporteres til en separat recovery-mappe. Hvis bare
  den avvikende varianten finnes, skal ordinær fil mangle i samlingen fremfor
  at databasen og filinnholdet gjøres inkonsistente.
- Recovery-mappen ved hel restore skal opprettes automatisk som en søstermappe
  til den gjenopprettede samlingen, bare når den trengs, og skal aldri
  overskrive en eksisterende mappe.
- `files.jsonl` skal lagre opprinnelig filendringstid per sti, og restore skal
  gjenopprette den så langt målfilsystemet støtter. Windows-opprettelsestid,
  ACL-er, eierdata og katalogtider er ikke del av førstversjonskontrakten.
- Første versjon skal bare oppdage og rapportere korrupte backupobjekter. Den
  skal ikke reparere, overskrive, flytte til karantene eller på annen måte
  endre dem.
- Vanlig snapshot skal avbryte uten publisering hvis et gjenbrukt objekt
  mangler, ikke er en vanlig fil eller har feil størrelse. Dette er en
  repository-/mediefeil, ikke et `degraded` kildeavvik.
- `create`, `check`, `list`, `problems`, `restore` og `restore-file` skal bruke
  én eksklusiv repositorylås i første versjon. En stale lås etter krasj skal
  aldri fjernes automatisk.
- `snapshot create --dry-run` skal være en rask, helt read-only plan uten full
  hashing eller låsfiler. Den skal validere og estimere, men tydelig si at
  hashavvik og endelige kopitall først finnes under reell kjøring.
- Første versjon skal bare støtte repository på lokale og eksterne disker.
  UNC-stier, SMB-mapper, NAS og andre nettverksmål skal avvises.
- FAT32 skal være et støttet repositorymål. Bildebank skal ikke avvise et helt
  filsystem på grunn av en teoretisk grense, men skal avbryte før skriving hvis
  en konkret fil overskrider målfilsystemets per-fil-grense.
- Snapshotkommandoen skal ha fire semantisk forskjellige resultater:
  `complete`, `degraded`, `recovery` og `failed`. `degraded` og `recovery` skal
  ha egne ikke-null exitstatuser selv om et snapshot ble publisert.
- Hel restore skal alltid bevare snapshotets opprinnelige `collection_id` og
  aldri gjøre den gjenopprettede kopien til en ny logisk samling automatisk.
- Snapshot kan få en valgfri, uforanderlig brukerkommentar ved opprettelse.
  Snapshot-ID og katalognavn skal fortsatt alltid genereres automatisk.
- `snapshot create` skal være tilgjengelig både i CLI og i launcheren i første
  versjon. Begge skal bruke samme underliggende plan- og opprettingskode.
- `snapshot check --full` skal også være tilgjengelig i launcheren i første
  versjon, med samme kontrollkode og resultatmodell som CLI-en.
- Full kontroll skal være helt read-only og skal ikke lagre tidspunkt eller
  resultat i repositoryet. Launcheren skal ikke gi tidsbaserte påminnelser om
  kontroll i første versjon.
- Reell restore skal vise plan og kreve eksakt tekstbekreftelse før skriving.
  `--yes` er et eksplisitt unntak for automatiserte eller avanserte
  arbeidsflyter.
- Hel restore kan opprette manglende målmappe etter bekreftelse når
  foreldremappen finnes. Tom målmappe kan brukes; ikke-tom, innkapslet eller
  repositorybasert målmappe skal avvises urørt.
- Hel restore skal bygges og kontrolleres i unike søsken-stagingmapper på
  samme filsystem. Eventuell recovery-mappe publiseres først og den ferdige
  samlingsmappen sist. Avbrutte stagingmapper med mediefiler skal aldri slettes
  automatisk.
- Enkeltfil-restore skal eksportere under filens opprinnelige relative sti,
  aldri overskrive eksisterende fil og avbryte ved kollisjon. Observerte
  avviksvarianter skal få tydelig hash-suffiks i filnavnet. Hvis både forventet
  og observert variant finnes, skal varianten velges eksplisitt.
- Bildebank skal ikke ha innebygd tidsplanlegging i første versjon. Snapshot
  startes manuelt fra CLI eller launcher; eventuell automatisering håndteres
  utenfor Bildebank.
- Første launcherutgave skal begrenses til snapshotoppretting og full kontroll.
  Snapshotliste, problemliste og restore skal være CLI-funksjoner i første
  versjon.
- En ukjent vanlig fil som ikke kan leses eller endres under kopiering, skal
  prøves én gang til. Hvis den fortsatt ikke kan sikres stabilt, skal filposten
  få konkret avviksstatus og snapshotet publiseres som `degraded` uten at en
  ustabil kopi presenteres som gyldig objekt.
- Symbolske lenker, junctions og andre reparse points skal oppdages og avvises.
- Alle logiske snapshotstier skal følge en portabel, Windows-sikker
  stikontrakt. Hele restoreplanen skal valideres for traversal, ugyldige navn og
  målfilsystemkollisjoner før én eneste utdatafil skrives.

## Ikke mål i første versjon

Første versjon skal ikke:

- automatisk slette gamle snapshots eller backupobjekter
- ha en `prune`- eller garbage collection-kommando som permanent sletter
  bildefiler
- automatisk reparere den aktive bildesamlingen
- reparere eller sette korrupte backupobjekter i karantene
- skrive tilbake til eller endre opprinnelige kildemapper
- støtte symbolske lenker, junctions eller andre reparse points i samlingen
- støtte repository på UNC-stier, SMB-mapper, NAS eller andre nettverksmål
- ha innebygd tidsplanlegging eller bakgrunnskjøring av snapshot
- love beskyttelse mot ransomware når backupmediet står tilkoblet og skrivbart
- erstatte behovet for flere backupmedier, frakoblet kopi og kopi utenfor
  boligen
- bevare Windows-opprettelsestid, NTFS-rettigheter og ACL-er, eierinformasjon
  eller katalogenes tidsstempler nøyaktig

Komprimering, Bildebank-kryptering og støtte for skylagring er ikke med i
første versjon. Disse egenskapene må ikke forsinke en enkel og kontrollerbar
lokal første versjon.

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

Første versjon skal bruke et append-only, innholdsadressert repository:

1. Hvert unikt filinnhold lagres som et objekt identifisert med SHA-256 og
   størrelse.
2. Et snapshotmanifest kobler relative stier og metadata til objektene som
   fantes da snapshotet ble tatt.
3. Et objekt publiseres først etter kopiering og kontroll av SHA-256.
4. Et snapshot publiseres først etter at alle tilgjengelige objekter og
   databasekopier er ferdige og verifiserte, og alle integritetsavvik er
   beskrevet i manifestet.
5. Et publisert snapshot endres aldri.
6. En avbrutt kjøring skal ikke kunne gjøre et tidligere snapshot ugyldig.

SHA-256 beskytter mot tilfeldig korrupsjon og feilkopiering, men er ikke alene
bevis på at en angriper ikke har endret både objekter og manifest. Beskyttelse
mot en angriper krever i praksis frakoblet eller skrivebeskyttet media, eller
senere støtte for signerte manifester med separat nøkkel.

## Foreløpig repository-format

Bildebank skal eie formatet. Følgende struktur er et forslag som må vurderes
før første formatversjon fryses:

```text
backup-repository/
  .bildebank-backup-repository.json
  .bildebank-repository.lock                 # bare mens en kommando kjører
  README.txt
  objects/
    sha256/
      ab/
        cd/
          abcdef...-123456
  snapshots/
    2026-07-15T183045Z-<snapshot-id>/
      manifest.json
      files.jsonl
      commit.json
  incomplete/
    <run-id>/
```

`PLASSERING` skal peke direkte på `backup-repository/` i eksemplet. Kommandoen:

```text
bildebank snapshot create D:\Backuper\Familiebilder
```

bruker dermed `D:\Backuper\Familiebilder` som repositoryrot. Bildebank kan
opprette den siste mappen når `D:\Backuper` finnes. Repositorymappen skal ikke
få navn automatisk fra samlingsmappen; den skal fortsatt være den samme hvis
samlingen senere flyttes eller får nytt navn.

`incomplete/<run-id>/` er det eneste stagingområdet for snapshotoppretting.
Hver reelle kjøring får en ny, unik `run-id`. Kandidatobjekter og
SQLite-stagingkopier lages der først; et objekt flyttes atomisk til sin
kanoniske sti under `objects/` først etter at størrelse og SHA-256 er
verifisert. Snapshotmappen bygges også der og flyttes atomisk til `snapshots/`
først når den har gyldig `commit.json`.

Et avbrudd kan dermed bare etterlate upubliserte data under `incomplete/`.
Bildebank skal aldri automatisk fortsette, endre, slette eller prune en slik
mappe. Etter en vellykket kjøring kan bare kjøreposten og tomme
stagingkataloger fjernes. `snapshot check` skal rapportere hver ikke-tomme
`incomplete/<run-id>/` med run-ID, alder og samlet størrelse, men ikke endre
den. Neste `snapshot create` skal starte en ny kjøring og kan bare gjenbruke
ferdig verifiserte objekter under `objects/`.

Initialisering skal følge disse reglene:

- Hvis repositorymappen ikke finnes og foreldremappen finnes, kan Bildebank
  opprette og initialisere den.
- Hvis repositorymappen finnes og er helt tom, kan Bildebank initialisere den.
- Hvis mappen er ikke-tom og mangler gyldig
  `.bildebank-backup-repository.json`, skal kommandoen avbryte uten å skrive
  eller flytte noe.
- Hvis metadatafilen finnes, skal format, `repository_id` og `collection_id`
  valideres før repositoryet brukes.
- Første versjon skal ikke ha `--adopt`. Et repository som ser skadet ut eller
  mangler metadata, skal undersøkes i stedet for å registreres automatisk på
  nytt.

`--dry-run` skal rapportere om en ny eller tom mappe ville blitt initialisert,
men skal ikke opprette mappe eller metadata.

Dry-run skal være en rask plan, ikke en full integritetskontroll. Den skal:

- validere samling, repositorymetadata og `collection_id`
- gjennomføre read-only kontroll for symbolske lenker og reparse points
- kontrollere at hoveddatabasen kan åpnes og leses
- kontrollere filtilstedeværelse og størrelse mot databasen
- bygge foreløpig databasekatalog, kontrollere eventuell absolutt
  face-databasekatalog og validere logiske stier og kollisjoner uten å åpne
  stagingkopier
- bruke databaseførte SHA-256-verdier og eksisterende objektstørrelser til å
  anslå hvilke objekter og hvor mange byte som må kopieres
- anslå nødvendig ledig plass
- kontrollere kjente filstørrelser mot målfilsystemets per-fil-grense, blant
  annet FAT32-grensen
- ikke beregne SHA-256 for alle kildefiler
- ikke opprette repositorymappe, metadata, stagingområde eller låsfil

Rapporten skal tydelig si at hashavvik, gjenbruk for ukjente filer og endelige
kopitall først kan avgjøres under den virkelige kjøringen. Resultatet kan også
endre seg hvis samlingen eller repositoryet endres etter dry-run.

Repositorymetadata skal minst ha:

- stabil `repository_id`
- `collection_id` for den eneste samlingen repositoryet tilhører
- samlingsnavn som informasjon til brukeren, uten at navnet brukes som
  identitet
- sist bekreftede maskinnavn og absolutte samlingssti
- `format_version`
- `required_features`, som er en tom liste i første versjon
- `created_by` og Bildebank-versjon
- opprettelsestidspunkt

`format_version` i repositorymetadata skal være heltallet `1` i første
versjon. En skriver skal bare legge til snapshots når den støtter den eksakte
repositoryversjonen og alle egenskaper som repositoryet erklærer som
påkrevd. Ukjente valgfrie JSON-felter skal ignoreres ved lesing og bevares når
den muterbare metadatafilen skrives på nytt. En egenskap som endrer nødvendig
lese- eller skriveatferd, krever en ny formatversjon eller en uttrykkelig
påkrevd egenskap; den kan ikke innføres som et felt eldre skrivere trygt
ignorerer.

Publiserte snapshotmapper skal være permanent skrivebeskyttet fra Bildebanks
side og aldri migreres. En fremtidig programversjon må lese gamle støttede
snapshotversjoner som de er. Bare den muterbare repositorymetadatafilen kan få
en senere, eksplisitt migrering. En slik migrering skal bruke tempfil, flush og
atomisk replace og skal aldri endre `repository_id`, `collection_id` eller
publiserte snapshots. Hvis oppdateringen av sist bekreftede maskin eller sti
avbrytes, skal enten hele gammel eller hele ny metadatafil være gjeldende; en
ufullstendig tempfil skal ikke tas i bruk automatisk.

Repositoryroten skal også ha en vanlig UTF-8-kodet `README.txt` som forklarer
formatversjonen, katalogstrukturen, at objektene inneholder rå ukomprimerte
byte, hvordan `files.jsonl` kobler objekter til opprinnelige relative stier,
hvordan `entry_id` og recovery-navn brukes, hvordan databasekatalogen brukes,
og hvordan `commit.json` kontrollerer manifestet og fillisten. Instruksjonen
skal være tilstrekkelig for teknisk manuell redning uten Bildebank, men trenger
ikke gjøre repositoryet direkte bla-bart som en vanlig bildemappe.

`manifest.json` skal minst ha:

- `format_version`, som er `1` i første snapshotformat
- `required_features`, som er en tom liste i første versjon
- `snapshot_id`, `collection_id` og repository-ID
- `collection_identity` med `source` og `verified`, slik at recovery ikke
  skjuler at identiteten eventuelt bare kom fra repositorymetadata
- valgfri brukerkommentar
- start- og sluttidspunkt
- Bildebank-versjon og schema-versjoner
- en `databases`-katalog med stabil rolle, logisk original- og restore-sti,
  schema- eller modellversjon når den finnes, nødvendig/regenererbar-status,
  objekt og om objektet er laget med SQLite backup-API
- antall filer og byte, samt SHA-256 og størrelse for `files.jsonl`
- samlet snapshotstatus: `complete`, `degraded` eller `recovery`
- eksplisitte eksklusjoner og eventuelle advarsler

Når hoveddatabasen består integritetskontrollen og ID-en leses som en gyldig
UUID, skal manifestet ha `collection_identity` med `source: "database"` og
`verified: true`. Et `recovery`-snapshot skal alltid ha
`source: "repository"` og `verified: false`, fordi det bruker den tidligere
bekreftede bindingen i repositorymetadata. En syntaktisk gyldig ID som kan
leses fra den skadede databasen kan bare sammenlignes med repositoryets ID;
den kan aldri gjøre identiteten verifisert eller binde et nytt repository. Feil
bare i en tilleggsdatabase gjør ikke identiteten uverifisert.

`files.jsonl` skal være UTF-8 med én selvstendig JSON-post per linje. En normal
post skal minst inneholde en unik, uforanderlig `entry_id`, portabel relativ
`path`, `record_type` og integritetsstatus. Når observerte byte finnes, skal
posten også ha objekthash, størrelse og opprinnelig filendringstid som
`mtime_ns`.
Poster med avvik skal ha forventede og observerte verdier som beskrevet under
integritetskontroll; manglende eller uleselige filer har ingen observert
objektreferanse. En `recovery_only`-post skal ha `entry_id`, `path: null`,
opprinnelig sti bare som
visningstekst og et programgenerert, portabelt recovery-navn. `entry_id` skal
genereres av Bildebank, være unik innen snapshotet og aldri avledes direkte fra
en utrygg sti. Manglende eller duplisert `entry_id` gjør snapshotmanifestet
ugyldig. Formatet skal kunne skrives og leses fortløpende uten at hele fillisten
må ligge i minnet.

`files.jsonl` arver formatreglene fra `manifest.json`; første versjon skal ikke
ha eget versjonsnummer per filpost. Råobjektene er de opprinnelige,
ukomprimerte byte og trenger heller ikke en separat objektkodingsversjon i
første format.

`commit.json` skal være den siste filen som lages i stagingmappen. Den skal
minst inneholde snapshot-ID, formatversjon, størrelse og SHA-256 for både
`manifest.json` og `files.jsonl`. Den skal selv være deterministisk UTF-8-JSON.
Et snapshot er bare publisert når snapshotmappen har en gyldig `commit.json`
som kontrollerer begge filene. Dette oppdager tilfeldig skade i selve
manifestet, men beskytter ikke mot en angriper som kan endre både innhold og
kontrollsummer.

En ferdig snapshotmappe med `manifest.json`, `files.jsonl` og `commit.json`
skal publiseres atomisk som siste steg. Status kan være
`complete`, `degraded` eller `recovery`. `complete` og `degraded` er normalt
gjenopprettbare snapshots; `degraded` betyr at minst én filpost eller
tilleggsdatabase har et integritetsavvik eller bare kan bevares som
redningsmateriale. `recovery` bevarer tilgjengelig innhold etter alvorlig feil
i hoveddatabasen, men er ikke en normalt gjenopprettbar bildesamling. En
avbrutt kjøring uten ferdig publisert snapshotmappe er ikke et publisert
snapshot. En separat, overskrivbar statusfil skal ikke være nødvendig for å
avgjøre dette.

### Nøyaktig v1-format

Dette avsnittet er den normative v1-kontrakten. Felt som ikke er oppført her,
kan legges til som valgfrie JSON-felter. De må ignoreres av lesere som ikke
kjenner dem og bevares når repositorymetadata skrives på nytt. Et nytt felt
som endrer nødvendig lese- eller skriveatferd, skal i stedet innføres gjennom
ny `format_version` eller en oppført `required_features`-verdi.

Alle JSON-filer og JSONL-linjer skal være UTF-8 uten BOM, bruke LF som
linjeslutt, ha objektfelter sortert etter Unicode-kodepunkt, ingen overflødig
mellomrom, og én avsluttende LF i vanlige JSON-filer. Strenger skal skrives som
vanlige UTF-8-JSON-strenger, med bare JSON-påkrevde escaping-tegn.
`files.jsonl` skal ha én slik kanonisk JSON-post per linje, uten tomme linjer;
en tom filliste er en tom fil. Disse reglene gjør at kontrollsummene og
v1-fixtures er stabile uavhengig av hvilken Bildebank-prosess som skrev dem.

Alle SHA-256-verdier er 64 små hex-tegn. `size_bytes` og `mtime_ns` er
desimalstrenger uten fortegn og uten ledende nuller, bortsett fra `"0"`.
Tidspunkter er UTC på formen `YYYY-MM-DDTHH:MM:SSZ`. `snapshot_id`,
`repository_id` og `collection_id` er små UUID-er med bindestreker.
Snapshotkatalogen heter `YYYY-MM-DDTHHMMSSZ-<snapshot_id>`, der tidspunktet er
snapshotets sluttid.

En objektreferanse har alltid nøyaktig denne formen:

```json
{"algorithm":"sha256","sha256":"<64 små hex-tegn>","size_bytes":"123456"}
```

Den fysiske objektstien utledes utelukkende av referansen:

```text
objects/sha256/<hash[0:2]>/<hash[2:4]>/<hash>-<size_bytes>
```

Objektet skal være en vanlig fil med nøyaktig den angitte størrelsen. Dermed
er både hash og størrelse en del av objektnøkkelen, slik planen forutsetter.
Å legge til størrelsen i filnavnet krever ingen ekstra lesing eller hashing,
fordi størrelsen allerede er kjent når objektet planlegges og verifiseres.

Repositorymetadatafilen skal ha nøyaktig disse påkrevde feltene:

```json
{
  "collection_id":"5d7b9c2e-6c4d-4d80-8a7f-857287c6c5d5",
  "collection_name":"Familiebilder",
  "created_at":"2026-07-15T18:30:45Z",
  "created_by":{"program":"bildebank","version":"1.0.0"},
  "format_version":1,
  "last_confirmed_source":{
    "collection_path":"D:\\Bilder\\Familiebilder",
    "confirmed_at":"2026-07-15T18:30:45Z",
    "machine_name":"FAMILIE-PC"
  },
  "repository_id":"a0c2e2ce-2920-44a5-9e39-2d3e24f08e73",
  "required_features":[]
}
```

`required_features` er en liste med små ASCII-navn. Den er tom i v1. En leser
eller skriver skal avvise repositoryet hvis listen inneholder et navn den ikke
støtter.

`manifest.json` skal ha disse påkrevde feltene:

```json
{
  "collection_id":"5d7b9c2e-6c4d-4d80-8a7f-857287c6c5d5",
  "collection_identity":{"source":"database","verified":true},
  "completed_at":"2026-07-15T18:30:45Z",
  "created_by":{"program":"bildebank","version":"1.0.0"},
  "databases":[],
  "exclusions":[],
  "files_jsonl":{"entry_count":"0","sha256":"<64 små hex-tegn>","size_bytes":"0"},
  "format_version":1,
  "note":null,
  "repository_id":"a0c2e2ce-2920-44a5-9e39-2d3e24f08e73",
  "required_features":[],
  "schema_versions":{"main":14},
  "snapshot_id":"a5696ae6-6799-4b4e-a842-430cf14f3484",
  "started_at":"2026-07-15T18:30:00Z",
  "status":"complete",
  "warnings":[]
}
```

`status` er nøyaktig én av `complete`, `degraded` og `recovery`.
`collection_identity` skal være nøyaktig ett av
`{"source":"database","verified":true}` og
`{"source":"repository","verified":false}`. Det første krever en
integritetskontrollert hoveddatabase; det andre krever `status: "recovery"` og
den tidligere bekreftede repositorybindingen. Andre kombinasjoner skal
avvises. `note` er enten `null` eller høyst 1 000 Unicode-tegn uten
kontrolltegn. `exclusions` og `warnings` er lister med forklarende strenger.
`schema_versions` er en påkrevd objektmapping fra databaserolle til
ikke-negativt heltall eller `null` når schemaet ikke kunne leses. Andre
valgfrie felt kan ha videre opplysninger om Bildebank eller databaser, men kan
ikke endre betydningen av feltene over.

Hver databasepost i `databases` skal ha `role`, `source_path_display`,
`restore_path`, `required`, `regenerable`, `capture`, `status`, `object`,
`schema_version` og `model_name`. `role` er `main`, `openclip`,
`face:<model_name>` eller `auxiliary:<relative-path>`. `capture` er
`sqlite_backup` for en gyldig konsistent databasekopi eller `raw_recovery` for
redningsmateriale. `status` er `ok`, `backup_failed` eller `unreadable`.
`object` er en objektreferanse eller `null`; `restore_path` er en normal
portabel sti eller `null` for `raw_recovery`. `schema_version` er et
ikke-negativt heltall eller `null`; `model_name` er streng eller `null`.
`required` og `regenerable` er JSON-boolske verdier. `source_path_display` er
bare informasjon og skal aldri brukes som restore-mål.

Hver post i `files.jsonl` skal ha disse feltene:

```json
{
  "entry_id":"e-000000000001",
  "expected":null,
  "integrity_status":"ok",
  "mtime_ns":"1721068245123456789",
  "object":{"algorithm":"sha256","sha256":"<64 små hex-tegn>","size_bytes":"123456"},
  "original_path_display":"2024/07/IMG_0001.jpg",
  "path":"2024/07/IMG_0001.jpg",
  "record_type":"file",
  "recovery_name":null,
  "restore_kind":"normal"
}
```

`entry_id` er `e-` etterfulgt av tolv sifre. Poster sorteres først på normal
`path`, deretter på `original_path_display` for `recovery_only`-poster, og får
løpende `entry_id` i denne rekkefølgen. `record_type` er `file` eller
`database_raw`; `restore_kind` er `normal` eller `recovery_only`.
`integrity_status` er én av `ok`, `missing`, `unreadable`, `hash_mismatch`,
`size_mismatch`, `changed_during_snapshot`, `unsafe_path` eller
`database_backup_failed`.

For en databaseført fil skal `expected` være
`{"sha256":"<64 små hex-tegn>","size_bytes":"123456"}`. For en ukjent
fil er den `null`. `object` er den observerte og lagrede objektreferansen, eller
`null` når ingen sammenhengende byte kunne sikres. `mtime_ns` er `null` når den
ikke kunne leses. En normal post har gyldig `path` og `recovery_name: null`.
En `recovery_only`-post har `path: null` og et `recovery_name` på formen
`entry-<tolv sifre>.bin`; det programgenererte navnet er det eneste navnet som
kan brukes ved eksport eller hel restore.

`commit.json` skal ha nøyaktig disse feltene:

```json
{
  "files_jsonl":{"sha256":"<64 små hex-tegn>","size_bytes":"123456"},
  "format_version":1,
  "manifest":{"sha256":"<64 små hex-tegn>","size_bytes":"123456"},
  "snapshot_id":"a5696ae6-6799-4b4e-a842-430cf14f3484"
}
```

`snapshot check` skal avvise et snapshot dersom en påkrevd v1-nøkkel mangler,
har feil type eller format, har ukjent enum-verdi, eller dersom objektstien ikke
stemmer med objektreferansen. Valgfrie, ukjente felt alene skal ikke gi avvik.

Kommandoresultatet skal skille mellom:

- `complete`: Snapshot er publisert uten kjente avvik og kommandoen returnerer
  vanlig suksess.
- `degraded`: Snapshot er publisert med fil- eller tilleggsdatabaseavvik og
  kommandoen returnerer en egen ikke-null exitstatus.
- `recovery`: Redningssnapshot er publisert etter feil i hoveddatabasen og
  kommandoen returnerer en annen, egen ikke-null exitstatus.
- `failed`: Ingen snapshot er publisert og kommandoen returnerer vanlig
  feilstatus.

CLI-kontrakten skal bruke exitkode `0` for publisert `complete`, `1` for
`failed` uten publisert snapshot, `2` for syntaks-/argumentfeil, `3` for
publisert `degraded` og `4` for publisert `recovery`. Launcheren skal bruke det
samme interne resultatobjektet, ikke tolke tekst eller starte CLI-en som
underprosess. Den skal vise henholdsvis fullført, opprettet med problemer,
recovery-snapshot opprettet eller feilet, og aldri presentere et publisert
`degraded` eller `recovery` snapshot som om ingen data ble sikret.

En SQLite-indeks eller et globalt repositoryregister kan brukes som
regenererbar hurtigbuffer, men snapshots skal kunne oppdages, kontrolleres og
gjenopprettes fra JSON- og JSONL-filene uten at en slik indeks finnes eller er
intakt.

Formatet skal gjøre det mulig å lage en framtidig read-only snapshot-browser.
En slik browser kan bruke `files.jsonl` som indeks og lese råobjektene med
filtype fra den opprinnelige stien. Snapshot-browser er ikke et krav i første
versjon.

Et eksisterende repository skal avvise snapshot fra en annen `collection_id`.
Samme samling kan ha separate repositories på flere backupmedier; hvert medium
får da sin egen `repository_id`, men samme `collection_id`.

En manuell kopi av hele samlingsmappen får samme `collection_id` som
originalen. Repositoryet kan derfor ikke sikkert skille en flyttet samling fra
to kopier som utvikler seg uavhengig. Hvis maskinnavn eller absolutt
samlingssti er forskjellig fra sist bekreftede arbeidssted, skal snapshotet
avbryte før repositoryet endres. Brukeren må eksplisitt bekrefte at dette er
samme logiske samling som er flyttet. Først da oppdateres arbeidsstedet i
repositorymetadataen. Hvis den gamle kopien senere brukes igjen, skal den
utløse samme kontroll.

### Objektnavn og kollisjoner

Objektnøkkelen skal bestå av algoritme, SHA-256 og størrelse. Hvis et objekt med
samme nøkkel allerede finnes, skal det kontrolleres før det gjenbrukes. Et
eksisterende objekt med feil størrelse eller feil hash er repositorykorrupsjon;
det skal aldri overskrives automatisk.

Ved vanlig snapshot skal et eksisterende objekt kontrolleres som vanlig fil og
mot forventet størrelse, men innholdet skal ikke hashes på nytt. Objektet ble
fullt hashverifisert da det først ble skrevet. Dette gjør ukentlige snapshots
praktiske. Stille korrupsjon som ikke endrer filstørrelsen, kan derfor først
oppdages av `snapshot check --full`. Programmet og dokumentasjonen skal være
tydelige om denne forskjellen.

Hvis et gjenbrukt objekt mangler, ikke er en vanlig fil eller har feil
størrelse, skal snapshotkjøringen avbryte uten å publisere eller skrive videre
til repositoryet. Feilen skal ikke gi `degraded`, fordi avviket ligger i
backupmålet og kan bety medie- eller filsystemskade. Meldingen skal anbefale
`snapshot check --full`, advare mot å stole på repositoryet som eneste backup
og anbefale snapshot til et annet medium når det er tilgjengelig.

Objektene skal lagres ukomprimert. Bilder og videoer er vanligvis allerede
komprimert, og direkte lagring gjør enkeltfiler lettere å kontrollere og redde
manuelt. Eventuell selektiv komprimering krever en senere formatversjon.

Objektene og manifestene skal ikke krypteres av Bildebank. Det gjør manuell
redning mulig uten Bildebank-nøkkel og unngår at nøkkeltap gjør hele backupen
uleselig. Brukeren kan beskytte backupmediet med kryptering som håndteres av
operativsystemet eller selve mediet.

Kopiering skal gå til en unik midlertidig fil på samme filsystem. Etter
verifisering får objektet endelig navn med en atomisk rename. Implementasjonen
skal ikke være avhengig av hardlinks, reflinks eller filsystem-snapshots.

Atomisk rename er ikke alene en garanti for at nylig skrevne data overlever
strømbrudd. Før et objekt, en metadatafil eller en snapshotmappe publiseres,
skal alle filhåndtak lukkes, filinnhold flushes med plattformens sterkeste
praktiske mekanisme, og foreldrekatalogen flushes der operativsystemet støtter
det. Staging og endelig plass skal alltid ligge på samme filsystem. Windows og
flyttbare medier kan fortsatt ha skrivecache utenfor programmets kontroll;
dokumentasjonen skal ikke love sterkere varighet enn operativsystemet og
mediet faktisk gir.

### Logiske stier i snapshotformatet

En normal `path` i `files.jsonl` skal være en relativ, portabel UTF-8-sti med
`/` som eneste separator. Den skal ikke være tom eller absolutt, inneholde
tomme komponenter, `.` eller `..`, bakstrek, kolon, kontrolltegn eller tegnene
`<`, `>`, `"`, `|`, `?` og `*`. Ingen komponent kan ende med punktum eller
mellomrom eller være et Windows-reservert navn som `CON`, `PRN`, `AUX`, `NUL`,
`COM1`–`COM9` eller `LPT1`–`LPT9`, heller ikke med filendelse. UNC-stier,
drive-prefiks og Windows device-stier er dermed heller ikke gyldige.

Under snapshotoppretting skal alle normale stier få en kanonisk nøkkel med
Windows-regler for separatorer og store/små bokstaver. To filer som kolliderer
på denne nøkkelen, kan ikke begge få en normal restore-sti. Hvis inventaret på
et annet filsystem faktisk inneholder en fil med ikke-portabel sti eller en
slik kollisjon, skal byte sikres som et tydelig `recovery_only`-objekt når
filen kan leses. Posten skal ha opprinnelig sti som visningstekst, `path: null`
og et programgenerert, portabelt recovery-navn. Snapshotet blir `degraded`.
En ugyldig databaseført sti skal aldri følges utenfor samlingen; den får avvik
uten observert objekt hvis ingen trygg inventarfil kan knyttes til den.

Ved restore skal alle normale `path`-verdier valideres på nytt; manifestet kan
være skadet eller manipulert. Før skriving skal hele settet med stier for
samlingsmappe, eksportmappe og eventuell recovery-mappe konverteres til
kanoniske nøkler etter reglene til det faktiske målfilsystemet. Traversal,
ugyldig komponent, duplikat eller kollisjon skal avbryte før første utdatafil
skrives. `recovery_only`-poster skal aldri bruke visningsteksten som målsti,
bare det programgenererte recovery-navnet.

## Hva et snapshot skal inneholde

Snapshotet skal inventere hele samlingsmappen, ikke bare radene i `files`. Det
gir mulighet til å gjenopprette samlingen selv om det finnes en ukjent fil eller
databasen er ufullstendig.

Snapshotet skal inkludere:

- alle aktive bilder og videoer
- alt under `deleted/`
- `.bilder.sqlite3`
- andre Bildebank-databaser som ligger i samlingen, blant annet databaser for
  søk og ansiktsmodeller
- kontrollerte ansiktsdatabaser i konfigurert absolutt `database_dir`, lagret
  med trygg restore-sti inne i den gjenopprettede samlingen
- nødvendige konfigurasjons- og metadatafiler i samlingen
- andre vanlige filer med mindre de er eksplisitt klassifisert som
  regenererbare eller runtime-filer

Kjente runtime-filer som target-lås og aktiv logg skal ikke tas med. Det må
lages én eksplisitt og testet liste over eksklusjoner. Ukjente filer skal som
hovedregel tas med og rapporteres, ikke ignoreres.

Eksklusjonslisten gjelder bare de programgenererte standardplasseringene:
`thumbs/` i roten, samt `.bildebank.lock`, `.bildebank.log`, `index.html`,
`image-search.html`, `personer.html` og `person-*.html` direkte i roten. Filer
med de samme navnene andre steder i samlingen skal tas med. Brukeren må regne
navnene og rotmappen `thumbs/` som reserverte for regenererbart innhold.

Databasefiler og SQLite-sidefiler som er klassifisert av databasekatalogen,
skal heller ikke kopieres en gang til av det vanlige filinventaret. Den
konsistente SQLite-kopien er normalvarianten; rå database- og sidefiler brukes
bare som særskilt redningsmateriale når databasekopien feiler.
En fil med endelsen `.sqlite3-wal`, `.sqlite3-shm` eller `.sqlite3-journal`
skal bare behandles som sidefil når den tilsvarende `.sqlite3`-filen finnes i
databasekatalogen. En foreldreløs fil med et slikt navn skal tas med og
rapporteres som en ukjent vanlig fil.

Genererte HTML-filer og thumbnails skal ikke tas med. De er regenererbare og
skal stå på den eksplisitte og testede eksklusjonslisten. Backupen skal ikke
betrakte dem som manglende filer eller integritetsfeil.
Dry-run og ordinær rapport skal vise antall og størrelse separat for
thumbnails, generert HTML og runtime-filer.

### Symbolske lenker og junctions

Symbolske lenker skal normalt ikke finnes i en Bildebank-samling, fordi
brukeren ikke skal flytte filer manuelt. Første versjon skal derfor bruke en
enkel regel: oppdag og avvis.

Før backupen skriver til repositoryet, skal en read-only forhåndskontroll lete
etter symbolske lenker, Windows-junctions og andre reparse points i hele
samlingsmappen. Kontrollen skal ikke følge lenkene. Hvis den finner én eller
flere, skal backupen avbryte og vise type, logisk sti og oppløst målsti når
denne kan bestemmes trygt.

Kontrollen skal både gå gjennom samlingstreet og kontrollere alle
mappekomponentene i databaseførte `files.target_path`. Det siste er nødvendig
fordi for eksempel `2020/05/bilde.jpg` kan ha en lenke i komponenten `2020`.

Etter forhåndskontrollen skal selve filinventaret fortsatt bruke
ikke-følgende filsystemoperasjoner. Hvis en lenke opprettes eller endres etter
forhåndskontrollen, skal kjøringen avbryte i stedet for å følge den.

Feilmeldingen skal forklare at lenker ikke støttes i versjonert backup og at
brukeren må gjøre samlingen selvstendig før backup kan tas. Støtte for et
konkret brukstilfelle, som en årsmappe flyttet til en annen disk, kan vurderes
senere hvis behovet faktisk oppstår.

## Integritetskontroll mot hoveddatabasen

For hver `files`-rad skal backupen finne den forventede filen under samlingen,
og kontrollere:

- at både den logiske og oppløste stien er innenfor samlingsmappen uten å
  krysse en lenke eller et reparse point
- at stien er en vanlig fil
- at størrelsen er lik `files.size_bytes`
- at SHA-256 er lik `files.sha256`

Dette gjelder både aktive og slettede rader. Hvis filen mangler eller har annet
innhold, skal backupen ikke betrakte det endrede innholdet som en ny, gyldig
utgave av den databaseførte filen.

Integritetsstatus skal finnes på to nivåer:

- Snapshotet merkes `degraded` når minst én databaseført fil har avvik.
- Hver filpost får sin egen status, for eksempel `ok`, `hash_mismatch`,
  `size_mismatch`, `missing` eller `unreadable`.

Ved hash- eller størrelsesavvik skal filposten registrere forventet
SHA-256 og størrelse fra `files`, samt observert SHA-256 og størrelse. De
observerte byte skal lagres som et eget objekt og kunne hentes ut for
undersøkelse. Hvis et tidligere snapshot allerede har objektet med forventet
SHA-256, skal også denne varianten forbli tilgjengelig. Det nye objektet skal
aldri erstatte det gamle.

Eksempel:

```text
uke 1: complete, bilde.jpg -> objekt A
uke 2: degraded, bilde.jpg forventet A, observert objekt B
```

Repositoryet inneholder da både A og B. Hvis forventet objekt A aldri har blitt
sikkerhetskopiert, kan B fortsatt bevares, men Bildebank må rapportere at den
forventede varianten ikke er tilgjengelig.

En manglende eller uleselig fil har ikke noe observert objekt, men resten av
snapshotet skal fortsatt publiseres som `degraded`. Det må finnes en read-only
oversikt over alle avvik og en eksplisitt måte å hente ut observerte og
tidligere forventede varianter til en mappe utenfor aktiv samling.

Verifiserte objekter som blir liggende uten referanse fordi en kjøring avbrytes
før snapshotmappen publiseres, skal beholdes permanent. En senere kjøring kan
gjenbruke dem etter kontroll. `snapshot check` skal rapportere antall og samlet
størrelse, men Bildebank skal ikke tilby cleanup eller prune som sletter dem.

Vanlige filer som finnes på disk, men ikke er databaseført, skal tas med med
nyberegnet SHA-256 og få en tydelig advarsel i rapporten. En ukjent fil gjør
ikke alene snapshotet `degraded`, så lenge filen kan leses og sikres. Det følger
prinsippet om at det er bedre å sikre én fil for mye enn én for lite.

For en ukjent vanlig fil skal størrelse og `mtime_ns` leses før og etter
kopiering. Hvis filen ikke kan leses, forsvinner eller endres under kopieringen,
skal Bildebank gjøre ett nytt kontrollert forsøk fra starten. Hvis filen fortsatt
ikke kan sikres stabilt, skal den få status `unreadable` eller
`changed_during_snapshot` i `files.jsonl`, uten objektreferanse til en mulig
sammenblandet kopi. Snapshotet skal publiseres som `degraded`, slik at resten
av samlingen fortsatt blir sikret.

## Konsistent snapshot av databaser og filer

Reell snapshot-oppretting skal holde samlingens `TargetLock` fra før første
databaseoppslag og filinventar til snapshotmappen er publisert eller
kjøringen har feilet. Det viderefører sikkerhetsmodellen til dagens backup og
hindrer andre Bildebank-kommandoer i å endre samlingen underveis.

Før kopiering skal Bildebank bygge én databasekatalog med disse rollene:

- `main` for `.bilder.sqlite3`; denne er nødvendig for normal hel restore
- `openclip` for `.bilder-openclip.sqlite3` når den finnes; innholdet er
  regenererbart, men skal likevel tas med
- `face:<model_name>` for hver kontrollert modellfil i den konfigurerte
  face-databasemappen; personer, bekreftelser og andre manuelle data gjør disse
  databasene ikke-regenererbare
- `auxiliary:<relative-path>` for andre SQLite-databaser som oppdages i
  samlingsmappen; ukjent rolle skal vises som en advarsel

Hver katalogpost skal ha stabil rolle, fysisk kildesti, portabel logisk
restore-sti, schema-versjon eller modellnavn når det finnes,
nødvendig/regenererbar-status og forventet restore-policy. Den fysiske
kildestien skal aldri brukes direkte som restore-mål.

Hvis `face_recognition.database_dir` er relativ, skal alle kontrollerte
modellfiler under den katalogen gjenopprettes til samme relative katalog. Hvis
den er absolutt, skal første versjon fortsatt sikre alle kontrollerte
modellfiler med SQLite backup-API, men gi dem restore-stier under
`.bildebank-faces/` i den gjenopprettede samlingen. Restore skal aldri skrive
til den gamle absolutte stien eller endre brukerens konfigurasjon automatisk.
Rapporten skal forklare at en fortsatt absolutt `database_dir` må vurderes og
eventuelt endres før face-funksjonene tas i bruk. En absolutt databasekatalog
skal være en vanlig lokal katalog uten lenker eller reparse points. Den og
repositoryet skal heller ikke ligge i hverandre. Hvis en av disse kontrollene
feiler, skal snapshotet avbryte før repositorydata skrives.

SQLite-databaser skal ikke kopieres som vanlige åpne filer. Det skal opprettes
en konsistent kopi gjennom SQLite backup-API til et stagingområde. Kopien skal
integritetskontrolleres og deretter lagres som et vanlig backupobjekt med den
logiske restore-stien fra databasekatalogen. Den aktive databasefilen og dens
`-wal`, `-shm` eller journalfil skal tas ut av det vanlige filinventaret, slik
at de ikke samtidig får en konkurrerende restorebetydning.

Hoveddatabasen skal kontrolleres både som kilde og etter at SQLite backup-API
har laget stagingkopien. Hvis kildedatabasen ikke kan åpnes eller har en
bekreftet SQLite-integritetsfeil, skal kjøringen fortsette i recovery-modus.
Den skal sikre alle lesbare vanlige filer og bevare rå `.bilder.sqlite3` med
eventuelle tilhørende SQLite-sidefiler som egne, tydelig merkede objekter når
de kan leses. Manifestet skal inneholde databasefeilen og hvilke råfiler som
ble bevart.

Ved bekreftet feil i hoveddatabasen skal recovery bare tillates når
repositoryet allerede er initialisert og lagret maskinnavn og absolutt
samlingssti stemmer med gjeldende arbeidssted. Snapshotet bruker repositoryets
`collection_id`, og manifestets `collection_identity` skal ha
`source: "repository"` og `verified: false`. Hvis en syntaktisk gyldig
`collection_id` fortsatt kan leses fra den skadede databasen, skal den være
lik repositoryets ID; ellers skal kjøringen avbryte uten å skrive data.
Hvis repositoryet er nytt eller tomt, eller arbeidsstedet ikke stemmer, skal
kjøringen avbryte uten å initialisere repositoryet. Første versjon skal ikke
ha `collection_id: unknown` eller en automatisk generert ID for recovery.

Hvis kildedatabasen er gyldig, men stagingkopien feiler på grunn av skrivefeil,
fullt medium eller korrupsjon på backupmålet, er dette ikke recovery-modus.
Kjøringen skal da feile uten å publisere snapshotet. Programmet skal aldri
feilmerke en målfeil som korrupsjon i kildesamlingen.

Et `recovery`-snapshot skal kunne brukes til å hente ut enkeltfiler og rå
databasefiler for undersøkelse. Vanlig hel restore til en ny Bildebank-samling
skal avvises fordi det ikke finnes en kontrollert, gyldig hoveddatabasekopi.

Hvis hoveddatabasen er gyldig, men SQLite backup eller integritetskontroll
feiler for en tilleggsdatabase, skal snapshotet publiseres som `degraded`.
Tilleggsdatabasens rå database- og sidefiler skal bevares som tydelig merkede
`recovery_only`-objekter når de kan leses. De skal ikke gjenopprettes som den
ordinære databasen eller få normal restore-sti. Restore skal bruke den gyldige
hoveddatabasen og rapportere hvilken tilleggsfunksjon som må kontrolleres,
gjenopprettes manuelt eller bygges opp igjen. En tilleggsdatabasefeil skal
aldri oppgraderes til `recovery` så lenge hoveddatabasen er gyldig.

Det finnes ingen felles transaksjon på tvers av alle databasefilene. Target-
låsen skal derfor hindre Bildebank fra å skrive til noen av dem mens snapshotet
bygges. Snapshotet publiseres først når alle databasekopiene og filobjektene er
ferdige.

Eksterne programmer kan fortsatt endre en bildefil uten å respektere låsen.
Backupen må derfor kontrollere størrelse og SHA-256 etter lesing. Hvis filen
endres under kopiering, skal den aktuelle kjøringen feile konservativt.

### Repositorylås

Første versjon skal bruke én eksklusiv repositorylås for `create`, `check`,
`list`, `problems`, `restore` og `restore-file`. En ny operasjon skal avbryte
med en forståelig melding hvis repositoryet allerede er i bruk. Dette betyr
blant annet at full kontroll og snapshotoppretting ikke kan kjøre samtidig.

`snapshot create --dry-run` er unntaket: den oppretter ikke låsfil og skal
heller ikke ta samlingens `TargetLock`. Den skal avbryte hvis den ser en
eksisterende repositorylås, men kan ikke garantere at tilstanden forblir
uendret etter kontrollen.

Låsfilen `.bildebank-repository.lock` skal minst inneholde kommando,
maskinnavn, prosess-ID og starttidspunkt. Den er runtime-tilstand og er ikke en
del av noe snapshot. Etter normal avslutning eller kontrollert avbrudd skal den
fjernes. Etter prosesskrasj eller strømbrudd skal den aldri fjernes automatisk;
brukeren må først kontrollere at ingen operasjon fortsatt kjører.

`snapshot create` skal ta repositorylåsen før samlingens `TargetLock`, og
frigjøre låsene i motsatt rekkefølge. Alle kodeveier som trenger begge låsene
skal bruke samme rekkefølge. For et manglende repository kan kommandoen etter
de innledende read-only stisjekkene opprette bare den eksakte repositorymappen
for å kunne opprette låsen eksklusivt. Metadata og backupdata skal ikke skrives
før begge låser er tatt og kilden er kontrollert. Hvis kjøringen stopper før
initialisering, kan den nyopprettede mappen bli stående tom; den skal fortsatt
kunne brukes ved neste kjøring.

## Foreslått kjøresekvens

1. Gjør read-only kontroll av aktiv samlingssti og repositoryplassering uten å
   konkludere om `collection_id` ennå. Kontroller at repositoryet ligger på
   lokal eller ekstern disk, har riktig type og ikke ligger i eller over
   samlingsmappen.
2. Forhåndsvalider eksisterende repositorymetadata. Hvis den eksakte
   repositorymappen mangler, opprett bare denne mappen. Ta deretter
   repositorylåsen med eksklusiv oppretting.
3. Valider repositorytilstanden og metadatafilen på nytt under låsen. Avvis en
   ikke-tom, uinitialisert mappe uten å flytte eller skrive andre filer.
   Låsfilen som denne kjøringen nettopp opprettet, skal ignoreres når en
   opprinnelig tom mappe vurderes.
4. Ta samlingens `TargetLock`.
5. Kjør read-only forhåndskontroll for lenker og reparse points. Avbryt før
   repositorymetadata, staging eller objekter skrives hvis noen finnes.
6. Kontroller hoveddatabasen, les `collection_id` når mulig, og avgjør om
   kjøringen er normal eller `recovery`. Bruk de særskilte recoveryreglene ved
   bekreftet feil i hoveddatabasen, også når en ID fortsatt kan leses.
7. Valider repositoryets `collection_id`, maskin og samlingssti under begge
   låser. Initialiser først nå metadata for et nytt repository, og bare i
   normal kjøring der hoveddatabasen besto integritetskontrollen og gyldig
   `collection_id` faktisk ble lest.
8. Opprett unik `run-id` og stagingområde.
9. Inventer samlingsmappen uten å følge lenker eller reparse points, bygg
   databasekatalogen og valider alle logiske stier.
10. I normal modus: les `files`, valider databaseførte stier, størrelser og
    SHA-256, og lag konsistente kopier av alle SQLite-databaser med SQLite
    backup-API.
11. I recovery-modus: sikre alle lesbare vanlige filer og rå databasefiler, og
    registrer hva som ikke kunne leses eller valideres.
12. Kopier og verifiser objekter som ikke allerede finnes gyldig i repository.
13. Bygg deterministisk `files.jsonl` og `manifest.json`, flush dem, bygg
    `commit.json` og kontroller alle referanser og kontrollsummer.
14. Flush staging og publiser snapshotmappen atomisk.
15. Skriv sluttrapport, frigjør `TargetLock` og frigjør deretter
    repositorylåsen.

Ved feil skal ingen tidligere snapshots eller objekter endres. En ny kjøring
skal kunne gjenbruke ferdig verifiserte objekter og ellers starte en ny,
uavhengig stagingkjøring.

## Kommandoer og overgang fra dagens mirror

Dagens `bildebank backup` har etablert betydning og eksisterende backupformat.
Det er farlig å tolke en gammel mirror-mappe som et nytt repository eller å
endre oppførselen lydløst.

Mulige senere alternativer etter brukertesting:

1. Behold `bildebank backup` som mirror og innfør `bildebank snapshot` for ny
   løsning.
2. Innfør underkommandoer som `bildebank backup mirror` og
   `bildebank backup create`, med en tydelig overgang for dagens syntaks.
3. Gjør `bildebank backup` til versjonert backup i en senere hovedversjon og
   flytt dagens funksjon til `bildebank mirror`.

Første implementasjon skal bruke alternativ 1: dagens `bildebank backup`
beholdes uendret som mirror, og den nye løsningen innføres som
`bildebank snapshot`. Det gir minst risiko mens format og arbeidsflyt prøves
ut. Navnet `snapshot` skal testes mot målgruppen. Hvis `backup` senere skal bli
navnet på den versjonerte løsningen, må det skje gjennom en tydelig overgang
der gammel syntaks aldri får ny betydning lydløst.

Et mulig første kommandosett er:

```text
bildebank snapshot create PLASSERING [--dry-run] [--note KOMMENTAR]
bildebank snapshot list PLASSERING
bildebank snapshot problems PLASSERING [SNAPSHOT-ID]
bildebank snapshot check PLASSERING [--full]
bildebank snapshot restore PLASSERING SNAPSHOT-ID NY-MAPPE [--dry-run]
bildebank snapshot restore-file
  PLASSERING SNAPSHOT-ID MÅLMAPPE
  (--path FILSTI | --entry-id ENTRY-ID)
  [--variant expected|observed] [--dry-run]
```

Alle skrivende restore-operasjoner skal ha dry-run. Hel gjenoppretting skal som
standard kreve en ny eller tom målmappe og aldri skrive over en eksisterende
bildesamling.

Før hel restore starter, skal CLI-en vise snapshot-ID, status, målmappe, antall
filer, datamengde og eventuell recovery-søstermappe. Brukeren må skrive en
eksakt bekreftelsestekst. Restore av enkeltfil skal tilsvarende vise valgt fil,
variant og eksportmappe før den krever egen tekstbekreftelse. `--yes` er eneste
unntak i første versjon. `--dry-run` skal aldri spørre om bekreftelse eller
skrive filer.

`--note` skal lagre en valgfri kommentar i `manifest.json`. Kommentaren skal
vises av `snapshot list`, i detaljvisning og før restore. Den er en del av det
atomisk publiserte snapshotet og kan derfor ikke redigeres senere i første
versjon. Snapshot-ID og mappenavn skal aldri komme fra kommentaren. CLI-en skal
ha en rimelig lengdegrense og avvise kontrolltegn; launcheren kan tilby et
valgfritt tekstfelt med samme grense.

`restore-file` skal kreve nøyaktig én av `--path` og `--entry-id`. Normal eller
forventet variant brukes uten `--variant` når posten bare har én ordinær
variant. Når både forventet og observert objekt finnes, skal kommandoen avbryte
uten eksplisitt `--variant`; `observed` skal alltid få hash-suffiks i
eksportnavnet. `recovery_only`-poster kan bare velges med `--entry-id` og skal
alltid eksporteres under det programgenererte portable recovery-navnet.
`problems` og restoreplanen skal vise både sti og `entry_id`, slik at også rå
databasefiler og andre redningsposter kan hentes uten manuell tolking av
objektlageret.

Den observerbare CLI-kontrakten i første versjon skal være:

- `snapshot create`: `0` for publisert `complete`, `3` for publisert
  `degraded`, `4` for publisert `recovery` og `1` for `failed` uten publisert
  snapshot
- hel restore: `0` når en kontrollert samling er publisert, også når
  observerte ekstravarianter er lagt i recovery-mappen; `3` når en bevisst
  ufullstendig samling er publisert fordi en forventet variant mangler; og `1`
  når ingen samlingsmappe er publisert
- `snapshot check`: `0` når kontrollen fullføres uten ny repositoryskade, `3`
  når kontrollen fullføres og finner manglende eller korrupte repositorydata,
  og `1` når kontrollen selv feiler eller avbrytes før resultatet er komplett
- alle kommandoer: `2` for syntaks- eller argumentfeil; vellykket `list` og
  `problems` returnerer `0` selv om listen viser tidligere kildeavvik

En vellykket dry-run returnerer `0`, og en avvist plan returnerer `1`. Dry-run
skal ikke returnere `3` eller `4` som en spådom om reell kjøring, fordi den ikke
har fullhashet kilden eller publisert noe.

Planer, fremdrift og sluttresultat skal gå til stdout. Advarsler og feil skal
gå til stderr. Launcheren skal bruke det felles interne resultatobjektet og
skal ikke tolke disse tekststrømmene. Første versjon skal ikke love et stabilt
`--json`-format; automatisering kan bruke dokumenterte argumenter og exitkoder.

### Launcherflyt for snapshot

Første versjon skal tilby `snapshot create` i launcheren. Flyten skal:

1. La brukeren velge den eksakte repositorymappen.
2. La brukeren skrive en valgfri kommentar.
3. Kjøre den raske dry-run-planen uten full hashing eller skriving.
4. Vise estimert antall nye objekter, byte, ledig plass og advarsler.
5. Be brukeren bekrefte reell kjøring.
6. Kjøre samme underliggende snapshotfunksjon som CLI-en.
7. Vise tydelig om resultatet ble `complete`, `degraded`, `recovery` eller
   `failed`, og om et snapshot faktisk ble publisert.

Launcheren skal ikke ha en egen implementasjon av repositoryformat,
integritetsregler eller kopiering. CLI og launcher skal dele planobjekter og
resultattyper.

### Launcherflyt for full kontroll

Første versjon skal også tilby `snapshot check --full` i launcheren. Flyten
skal:

1. La brukeren velge et eksisterende repository.
2. Forklare at alle objekter blir lest og at kontrollen kan ta lang tid.
3. Ta den vanlige eksklusive repositorylåsen.
4. Vise fremdrift med antall objekter og byte.
5. Kunne avbrytes kontrollert uten å endre repositoryet.
6. Vise om repositoryet er helt kontrollert, kontrollen ble avbrutt, eller det
   finnes manglende eller korrupte objekter.
7. Vise berørte snapshot-ID-er og logiske filstier ved avvik.

CLI og launcher skal bruke samme read-only kontrollimplementasjon.

Kontrollhistorikk skal ikke lagres i repositoryet i første versjon. Dermed kan
launcheren ikke vite eller advare om når full kontroll sist ble kjørt. Dette
holder kontrollkommandoen uten vedvarende endringer i repositoryet.

Snapshotliste, detaljert problemliste, restore av enkeltfil og hel restore skal
være CLI-only i første versjon. Restore er sjeldent og har flere sikkerhetsvalg;
før det får launchergrensesnitt, skal CLI-kontrakten, dry-run og
brukerdokumentasjonen være gjennomtestet.

## Gjenoppretting

En backup er ikke ferdig designet før restore er spesifisert og testet.

Hel restore skal:

- validere repositorymetadata, `commit.json`, snapshotmanifest og alle
  nødvendige objekter
- vise valgt samling, snapshotdato, antall filer og plassbehov
- avvise eksisterende ikke-tom målmappe som standard
- opprette manglende målmappe først etter bekreftelse når foreldremappen finnes
- avvise målmappe inne i aktiv samling eller repository, og avvise målmappe som
  inneholder aktiv samling eller repository
- validere alle normale og recovery-baserte målstier og kollisjoner samlet før
  skriving
- bygge samlingen og eventuell recovery-mappe i en unik stagingrot som er
  søster til målet og ligger på samme filsystem
- kopiere via midlertidige filer i staging og verifisere SHA-256 etter
  kopiering
- gjenopprette opprinnelige relative stier, inkludert `deleted/`
- sette tilbake filendringstid fra `mtime_ns` så langt målfilsystemet støtter
- gjenopprette databasekopiene som vanlige SQLite-filer
- bevare opprinnelig `collection_id` i hoveddatabasen
- ikke kopiere repositorymetadata inn i den gjenopprettede samlingen
- kjøre database- og filintegritetskontroll før samlingen tas i bruk
- skrive en tydelig rapport, men ikke automatisk reparere avvik

Stagingroten skal ha unik `run-id`, en liten kjørepost og separate undermapper
for samling og eventuell recovery. Alle data, databaser, stier og hasher skal
kontrolleres ferdig der. Hvis brukeren valgte en eksisterende tom målmappe,
skal den kontrolleres på nytt rett før publisering og bare fjernes med en
operasjon som lykkes dersom den fortsatt er tom. Hvis den ikke lenger er tom,
skal publisering avbrytes og staging beholdes.

Når recovery-mappe trengs, skal den publiseres med rename først. Den ferdig
kontrollerte samlingsmappen publiseres med rename sist; at denne endelige
mappen finnes er definisjonen på en publisert hel restore. Etter vellykket
publisering kan bare kjørepost og tomme stagingkataloger ryddes. Et avbrudd før
samlingsmappen er publisert gir exitkode `1`, også hvis en recovery-mappe
allerede rakk å bli publisert. Neste restorekommando skal oppdage kjente
staging- og recoveryrester, avbryte før ny skriving og forklare hva som er
fullført. Første versjon skal ikke automatisk fortsette, overskrive eller slette
slike mapper; brukeren må beholde dem eller flytte dem eksplisitt før et nytt
forsøk.

Før restore starter, skal Bildebank kontrollere repositoryets sist bekreftede
maskin og samlingssti. Hvis den opprinnelige samlingen fortsatt ser ut til å
finnes, skal brukeren advares om at original og gjenopprettet kopi har samme
`collection_id` og ikke bør brukes parallelt som uavhengige samlinger. Restore
skal aldri generere en ny ID automatisk. Første snapshot fra den gjenopprettede
plasseringen skal utløse den vanlige eksplisitte bekreftelsen på at samme
logiske samling er flyttet.

Restore av enkeltfil skal kreve eksplisitt snapshot og enten normal filsti
eller `entry_id`, og eksplisitt variant når både forventet og observert objekt
finnes. Standardmålet skal være en vanlig eksportmappe utenfor aktiv samling.
Restore direkte inn i aktiv samling skal ikke være med i første versjon, fordi
det krever koordinert oppdatering av både filsystem og database.

Eksportmappen kan opprettes etter bekreftelse når foreldremappen finnes. En
eksisterende eksportmappe kan brukes, men restore skal legge filen under dens
opprinnelige relative sti og avbryte hvis den endelige stien allerede finnes.
Observerte avviksvarianter skal få en hash-suffiks før filendelsen. Målet skal
ligge utenfor aktiv samling og repository.

For en filpost med integritetsavvik skal brukeren kunne hente ut både
observert variant og tidligere forventet variant når begge finnes.

Ved hel restore av et `degraded` snapshot gjelder denne standarden:

- Hvis forventet variant finnes i repositoryet, gjenopprettes den til ordinær
  plass slik at den samsvarer med hoveddatabasen.
- Observert avvikende variant eksporteres til en separat recovery-mappe for
  undersøkelse og plasseres ikke i den gjenopprettede bildesamlingen.
- Hvis forventet variant ikke finnes, skal filen mangle på ordinær plass.
  Observert variant eksporteres fortsatt til recovery-mappen, og restore skal
  rapportere tydelig at den gjenopprettede samlingen er ufullstendig og
  returnere exitkode `3` etter publisering.

Recovery-mappen skal opprettes automatisk som en søstermappe og bare hvis minst
én observert variant eller `recovery_only`-post må eksporteres dit. Dette
omfatter blant annet rå tilleggsdatabasefiler og lesbare filer uten portabel
normalsti. Hvis den gjenopprettede samlingen er:

```text
C:\Gjenopprettet\Familiebilder
```

kan recovery-mappen for eksempel være:

```text
C:\Gjenopprettet\Familiebilder-recovery-20260715
```

Navnet skal inneholde snapshotdato eller snapshot-ID og aldri kollidere med
eller overskrive en eksisterende mappe. Ved kollisjon skal restore avbryte før
den skriver filer. For normale stier skal recovery-mappen bevare opprinnelig
relativ mappestruktur og merke observerte varianter tydelig i filnavnet.
`recovery_only`-poster skal bruke sitt programgenererte portable navn.
Recovery-mappen skal inneholde en UTF-8-rapport med opprinnelig visningstekst,
grunn til recoveryplassering og forventet og observert SHA-256 når de finnes.
Den skal ligge utenfor bildesamlingen slik at innholdet ikke senere tas med som
vanlige samlingsfiler.

## Kontroll av backupen

Det skal finnes to kontrollnivåer:

- En rask kontroll validerer repositorystruktur, formatversjoner,
  `commit.json`, kontrollsummene for manifest og `files.jsonl`, objektnavn,
  størrelser og at alle refererte objekter finnes.
- En full kontroll leser alle objekter i repositoryet, også urefererte
  verifiserte objekter, og beregner SHA-256 på nytt.

Full kontroll kan ta lang tid og må vise fremdrift. Resultatet skal skille
mellom:

- komplett og verifisert snapshot
- `degraded` snapshot med konkrete filavvik
- `recovery`-snapshot med databasefeil og tilgjengelig redningsinnhold
- manglende objekt
- objekt med feil størrelse eller hash
- ugyldig eller uleselig manifest
- manglende, ugyldig eller avvikende `commit.json`
- ufullstendig kjøring som aldri ble publisert
- ureferert objekt som ikke gjør eksisterende snapshots ugyldige

For hvert manglende eller korrupt objekt skal kontrollen finne og vise alle
snapshot-ID-er og logiske filstier som refererer til objektet. Snapshotets
publiserte status og manifest skal ikke endres; kontrollrapporten beskriver
dets nåværende gjenopprettbarhet. Status som `complete` beskriver tilstanden da
snapshotet ble publisert, ikke en garanti mot senere skade på backupmediet.

Kontrollen skal være read-only. Første versjon skal ikke ha automatisk eller
manuell repair, overskriving eller karantene. En senere eksplisitt
reparasjonsflyt kan vurderes, men må hente en verifisert variant fra aktiv
samling eller et annet repository og aldri slette den korrupte varianten.

## Kapasitet og ytelse

Første snapshot må lese og hashe hele samlingen. Senere snapshots kan unngå å
kopiere objekter som allerede finnes, men hver kjøring skal fortsatt lese og
beregne SHA-256 for alle databaseførte mediefiler før snapshotet publiseres.

Eksisterende backupobjekter skal ikke fullhashes som del av vanlig
snapshotoppretting. De kontrolleres på type og størrelse. Dermed leser en
ukentlig kjøring hele kildesamlingen, men ikke i tillegg hele backupmediet.
Brukeren skal enkelt kunne starte `snapshot check --full` separat når det er
tid til en full gjennomlesing av repositoryet. Kontrollen skal vise fremdrift
og være trygg å avbryte uten å endre repositoryet.

Tidsbruken skal måles i Windows-piloten. Kontrollert caching basert på
størrelse, mtime eller filidentitet skal ikke være med i første versjon. En slik
optimalisering kan vurderes senere dersom full kontroll faktisk er for treg,
men må da utformes slik at stille korrupsjon fortsatt blir oppdaget.

Repositoryet skal anslå nødvendig ledig plass før kopiering, men beregningen blir et
estimat fordi komprimering, samtidige endringer og filsystemoverhead kan
variere. Full disk skal gi en ufullstendig kjøring uten å skade eldre
snapshots.

Lokale og eksterne disker med FAT32 skal støttes. Før reell kopiering skal
Bildebank kontrollere kjente kildefiler og planlagte repositoryfiler mot
målfilsystemets maksimale filstørrelse. Hvis én konkret fil er for stor, skal
kjøringen avbryte før repositoryet endres og vise filen og grensen. FAT32 skal
ikke avvises når alle filer er små nok. Tidsoppløsning og andre ufarlige
metadataforskjeller håndteres som beskrevet under restore.

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
- ny snapshotkjøring skal beregne SHA-256 på nytt selv når størrelse og mtime
  er uendret
- `degraded` snapshot med forventet og observert objekt, og uthenting av begge
  varianter for undersøkelse
- manglende forventet objekt når bare observert variant kan sikres
- hel restore med forventet variant i samlingen og observert variant i separat
  recovery-mappe
- hel restore der forventet variant mangler og ordinær fil derfor ikke
  opprettes
- automatisk recovery-søstermappe, oppretting bare ved behov og avvisning ved
  navnekollisjon uten overskriving
- hel restore og restore av enkeltfil med tekstbekreftelse, samt `--yes` og
  `--dry-run` uten bekreftelsesprompt
- enkeltfil-restore med relativ eksportsti, oppretting av manglende
  eksportmappe, kollisjonsavbrudd og hash-suffiks for observert variant
- hel restore til manglende og tom målmappe, samt avvisning av ikke-tom,
  innkapslet eller repositorybasert målmappe uten skriving
- restore av `mtime_ns` på filsystem med full og avrundet tidsoppløsning, uten
  at avrunding behandles som korrupt filinnhold
- kontroll av at ACL-er, eierdata, Windows-opprettelsestid og katalogtider ikke
  inngår i førstversjonsløftet
- hel restore bevarer `collection_id`, advarer når originalstien fortsatt
  finnes og utløser flyttebekreftelse ved neste snapshot
- eksisterende backupobjekt med feil innhold
- vanlig snapshot skal oppdage manglende objekt og feil størrelse uten å
  fullhashe gjenbrukte objekter
- manglende eller feil størrelse på gjenbrukt objekt skal avbryte uten nytt
  snapshot og uten videre repositorieskriving
- korrupsjon med uendret størrelse skal oppdages av `snapshot check --full`,
  ikke nødvendigvis av vanlig snapshot
- korrupsjonsrapport skal liste alle berørte snapshots og filstier uten å
  endre manifest eller objekt
- kontrollen skal ikke tilby repair, overskriving eller karantene
- stor `files.jsonl` som skrives og leses fortløpende
- ugyldig JSON-linje og avvik mellom `manifest.json` og SHA-256 for
  `files.jsonl`
- teknisk manuell uthenting av en fil ved hjelp av `README.txt`, `files.jsonl`
  og råobjektet, uten Bildebank-programmet
- ukjent mediafil som ikke finnes i `files`
- genererte standardfiler ekskluderes bare i roten, mens samme filnavn i en
  annen mappe tas med
- SQLite-sidefil til en katalogført database tas ut av normalinventaret, mens
  en foreldreløs sidefil tas med og rapporteres som ukjent
- ukjent fil som er uleselig, forsvinner eller endres under kopiering, både med
  vellykket nytt forsøk og med `degraded` etter andre feil
- kontroll av at en ustabil midlertidig kopi ikke publiseres som gyldig objekt
- konsistent SQLite-kopi, også med WAL/journal i bruk
- bekreftet korrupt eller uleselig hoveddatabase som publiserer
  `recovery`-snapshot med rå databasefiler og lesbare vanlige filer
- korrupt eller uleselig tilleggsdatabase som publiserer `degraded`, bevarer
  råfilene og lar hoveddatabasen gjenopprettes normalt
- `recovery`-snapshot som tillater uthenting av filer, men avviser vanlig hel
  restore
- forskjellige kommandoresultater og launcherstatus for `complete`,
  `degraded`, `recovery` og `failed`, inkludert om snapshot ble publisert
- snapshot med og uten valgfri kommentar, visning i liste og restore, samt
  avvisning av for lang kommentar og kontrolltegn
- launcherflyt med mappevalg, kommentar, rask dry-run, avbrutt bekreftelse og
  alle fire sluttresultater uten duplisert snapshotimplementasjon
- launcherflyt for `snapshot check --full` med tidsadvarsel, fremdrift,
  kontrollert avbrudd og samme avviksrapport som CLI
- full kontroll som ikke lagrer kontrollhistorikk eller gir tidsbaserte
  launcherpåminnelser
- kontroll av at snapshotliste, problemliste og restore ikke eksponeres i
  første launcherutgave
- gyldig kildedatabase kombinert med skrivefeil eller korrupt stagingkopi på
  backupmålet; kjøringen skal da feile uten publisert snapshot
- avbrudd under objektkopiering og før snapshotpublisering
- avbrutt snapshot med data under `incomplete/<run-id>/`; kontroll skal
  rapportere mappen, og ny kjøring skal verken endre, gjenoppta eller slette den
- full disk og andre skrivefeil
- ugyldig repository-ID eller feil `collection_id`
- oppretting i eksakt repositorymappe, manglende foreldremappe og
  repositoryplassering i eller over samlingsmappen
- avvisning av UNC-sti, SMB-mappe, NAS og annet oppdaget nettverksmål
- vellykket snapshot og restore på FAT32 med små filer, samt avbrudd før
  skriving når én fil overskrider målfilsystemets per-fil-grense
- initialisering av manglende og tom mappe, samt avvisning av ikke-tom mappe
  uten gyldig metadata uten noen endringer
- rask `--dry-run` uten full hashing, katalogoppretting, metadata eller
  låsfiler, med tydelig usikkerhetsrapport
- endret maskin eller samlingssti, både avbrutt og eksplisitt bekreftet som
  flyttet samling
- konkurrerende repositoryoperasjoner, kontrollert opprydding av lås og stale
  lås etter prosesskrasj som ikke fjernes automatisk
- symbolsk fil- og kataloglenke, Windows-junction, brutt lenke og andre reparse
  points; alle skal avbryte før repositoryet endres
- Unicode, mellomrom, lange stier og store/små bokstaver på Windows
- rask og full `check`
- hel restore og restore av enkeltfil
- restore til eksisterende eller for liten målmappe
- kontroll av at gamle snapshots fortsatt kan gjenopprettes etter nye kjøringer
- fryste format-v1-fixtures som verifiserer kanoniske JSON- og JSONL-byte,
  utledning av objektsti fra hash og størrelse, alle påkrevde felt, enum-verdier
  og null-former; dessuten ignorering og bevaring av ukjente valgfrie
  JSON-felter, avvisning av ukjent `required_features`-verdi og avvisning av nyere
  inkompatibel repository- eller snapshotversjon
- kontroll av at en eldre skriver ikke kan legge snapshot til et repository
  med format eller påkrevd egenskap den ikke forstår
- `recovery` med uleselig eller bare syntaktisk lesbar `collection_id` mot et
  allerede bundet repository på samme arbeidssted, kontroll av ID-likhet når
  den kan leses, samt avvisning mot nytt/tomt repository og mot endret
  arbeidssted
- databasekatalog med hoveddatabase, OpenCLIP, flere face-modeller, ukjent
  SQLite-database og absolutt `face_recognition.database_dir`
- avvisning av absolutt face-databasekatalog på nettverksmål, gjennom lenke
  eller i overlapp med repositoryet
- kontroll av at en konsistent databasekopi får normal restore-sti, mens rå
  database- og sidefiler ved feil bare blir `recovery_only`
- path traversal, absolutt sti, UNC-/device-sti, NTFS alternativ datastrøm,
  Windows-reserverte navn, avsluttende punkt/mellomrom og to ulike poster som
  kolliderer etter normalisering eller store/små bokstaver
- manglende og duplisert `entry_id`
- ikke-portabel kildefil som sikres som `recovery_only` uten at visningsstien
  noen gang brukes som restore-mål
- enkeltfil-restore som krever eksplisitt valg mellom `expected` og `observed`
  når begge finnes
- enkeltfil-restore med `--entry-id` for rå databasefil og annen
  `recovery_only`-post, uten bruk av opprinnelig visningssti som mål
- avbrudd før og etter hvert publiseringstrinn for hel restore, også mellom
  publisering av recovery-mappe og samlingsmappe, med gjenkjenning og bevaring
  av alle ufullstendige mapper
- restore til en eksisterende tom målmappe som blir fylt av en annen prosess
  før publisering; staging skal beholdes og ingen fil overskrives
- manglende eller korrupt `commit.json`, endret `manifest.json` med uendret
  `files.jsonl`, og avbrudd under atomisk oppdatering av repositorymetadata
- injiserte avbrudd rundt flush og rename, og kontroll av at et snapshot uten
  gyldig commit-post aldri regnes som publisert
- test som håndhever repositorylås før `TargetLock` i alle kodeveier og motsatt
  rekkefølge ved frigjøring
- eksakte exitkoder og stdout/stderr-regler for snapshot, check og restore,
  inkludert publisert ufullstendig restore

Før bruk på den virkelige samlingen må det gjennomføres en Windows-test med et
lite, representativt datasett på samme type backupmedium som skal brukes.
Testen skal omfatte avbrudd og faktisk gjenoppretting, ikke bare oppretting av
backup.

## Foreslåtte implementasjonstrinn

Implementeringsstatus 2026-07-16:

- Trinn 0 og 1 er ferdige. `snapshot create --dry-run` er implementert og
  skrivefri.
- Trinn 2 er påbegynt. Eksklusiv repositorylås, initialisering, unike
  `incomplete/<run-id>`-områder, verifisert objektlagring og konsistent kopi
  med SQLite backup-API er implementert som testede grunnprimitiver.
  Deterministisk, strømmet `files.jsonl`, `manifest.json`, `commit.json`,
  kontroll av objektreferanser og atomisk append-only-publisering er også
  implementert og testet.
- Normal samlingsbygging er implementert for databaseførte, slettede og
  ukjente filer, inkludert observerte hash-/størrelsesavvik, manglende filer,
  kontrollert nytt forsøk og databasekatalog med hoveddatabase, OpenCLIP,
  face-modeller og andre SQLite-databaser.
- `raw_recovery` ved bekreftet kildefeil i en tilleggsdatabase er implementert:
  lesbare database- og sidefiler bevares som `recovery_only`, mens mål- og
  stagingfeil fortsatt avbryter uten publisering.
- Repositorybundet `recovery` ved åpne-, lese- eller integritetsfeil i
  hoveddatabasen er implementert og testet. Det krever tidligere bekreftet
  maskin, absolutt samlingssti og `collection_id`, bevarer lesbare vanlige
  filer og rå databasefiler og publiserer identiteten som ikke verifisert.
- Full kjøreorkestrering og reell CLI-kjøring av `snapshot create` er
  implementert. Repositorylåsen tas før samlingslåsen, ny initialisering skjer
  først etter vellykket hoveddatabasekontroll, og publiserte resultater bruker
  exitkode `0`, `3` eller `4` for henholdsvis `complete`, `degraded` og
  `recovery`.
- Den samme interne plan- og opprettingsfunksjonen er koblet til launcheren via
  en bakgrunnsjobb som ikke fryser vinduet. Launcheren viser alltid en
  skrivefri plan og krever bekreftelse før oppretting, og skiller tydelig
  mellom `complete`, `degraded`, `recovery` og feil uten å tolke CLI-tekst.
- Trinn 2 er fullført.
- Snapshotliste, rask kontroll og full SHA-256-kontroll er implementert med en
  felles, read-only kontrollmotor. `list` og `check` er uavhengige av aktiv
  bildesamling, bruker eksklusiv repositorylås og endrer ikke repositoryet
  utover den midlertidige låsfilen. Kontrollen validerer v1-metadata fortløpende,
  rapporterer ufullstendige kjøringer og urefererte objekter, og kobler manglende
  eller korrupte objekter til alle berørte snapshot-ID-er og logiske stier.
- Full kontroll er koblet til launcheren med samme resultatmodell, fremdrift i
  objekter og byte og kontrollert avbrudd. Ingen kontrollhistorikk lagres.
- Trinn 3 er fullført.
- Trinn 4 er påbegynt med en felles, skrivefri plan for hel restore og
  enkeltfil-restore. Planen validerer valgt snapshot og nødvendige objekter,
  mål- og eksportplasseringer, alle ordinære og recovery-baserte utstier,
  variantvalg, plassbehov, recovery-navnekollisjon og rester etter tidligere
  restoreforsøk. `restore --dry-run` og `restore-file --dry-run` er koblet til
  CLI uten krav om aktiv bildesamling og uten bekreftelsesprompt eller skriving.
- Reell hel restore er implementert med eksakt tekstbekreftelse eller `--yes`,
  unik søsken-staging, SHA-256-verifisering under kopiering og i en avsluttende
  kontroll, gjenoppretting av filenes `mtime_ns`, kontroll av hoveddatabasen og
  opprinnelig `collection_id`, og flush før atomisk publisering. Eventuell
  recovery-mappe publiseres først og samlingsmappen sist. Måltilstanden
  kontrolleres på nytt før publisering, og rester etter feil eller avbrudd
  bevares og stopper senere restoreforsøk til de er undersøkt. En bevisst
  ufullstendig restore publiseres med exitkode 3.
- Reell hel restore er manuelt verifisert på Linux med et faktisk snapshot,
  etterfulgt av `doctor --deep` på den gjenopprettede samlingen. Alle 249
  databaseførte mediefiler fantes, ingen orphan-filer ble funnet og SHA-256
  stemte for alle filene.
- Reell enkeltfil-restore er implementert med samme låste snapshotvalidering
  som dry-run, eksakt tekstbekreftelse eller `--yes`, kontroll av ledig plass
  og målfilsystemets filstørrelsesgrense, og eksklusiv oppretting som aldri
  overskriver en eksisterende fil. Eksporten bevarer relativ sti og `mtime_ns`,
  verifiserer SHA-256 under og etter kopiering og lar eventuelle mapper eller
  ufullstendige utdata stå urørt etter feil. Forventet og observert variant,
  hash-suffiks og `recovery_only` via `entry_id` er testet. En faktisk
  enkeltfil-restore er manuelt sammenlignet byte-for-byte med originalfilen på
  Linux, og et nytt forsøk mot samme mål ble avvist uten overskriving.
- Trinn 4 er fullført.
- Trinn 5 er ikke påbegynt.

### Trinn 0 – Enighet om design

- Beskriv førstversjonen med `snapshot` ved siden av uendret `backup`-mirror,
  og planlegg brukertest av navnene.
- Frys første versjon av repository- og manifestformatet.
- Frys kompatibilitetsreglene, den portable stikontrakten, databasekatalogen og
  CLI-resultatkontrakten.
- Avgjør om andre regenererbare filer enn HTML og thumbnails kan utelates.
- Beskriv restorekontrakten fullstendig.

### Trinn 1 – Read-only plan og inventar

- Implementer repositoryvalidering og `--dry-run`.
- Lag filinventar og sammenligning mot `files`.
- Rapporter nødvendig plass, ukjente filer og integritetsavvik.
- Ikke skriv backupdata i dette trinnet.

### Trinn 2 – Objekter og atomisk snapshot

- Implementer staging, verifisert objektkopiering og manifest.
- Implementer `commit.json`, atomisk metadataoppdatering og flush før
  publisering.
- Implementer konsistent SQLite-backup.
- Test alle avbruddsgrenser og at eldre snapshots aldri endres.
- Koble samme plan- og opprettingsfunksjon til CLI og launcher.

### Trinn 3 – Kontroll

- Implementer snapshotliste, rask kontroll og full SHA-256-kontroll.
- Gjør kontrollen uavhengig av aktiv bildesamling.
- Koble full kontroll til launcheren uten å duplisere kontrollkoden.

### Trinn 4 – Gjenoppretting

- Implementer full stivalidering, dry-run og stagingbasert hel restore til ny
  eller tom mappe.
- Implementer konservativ oppdagelse av avbrutte restoremapper uten automatisk
  sletting eller overskriving.
- Implementer restore av enkeltfil med eksplisitt variantvalg til eksportmappe.
- Test faktisk bruk av en gjenopprettet samling.

### Trinn 5 – Dokumentasjon og Windows-pilot

- Skriv brukerdokumentasjon med Windows-stier og uten krav om teknisk
  forkunnskap.
- Kjør pilot mot lite testsett på Windows 11 og eksternt medium.
- Dokumenter verifisering, diskrotasjon og øvelse på restore.

## Åpne beslutninger

Disse punktene bør behandles eksplisitt i neste iterasjoner:

1. Viser brukertesting at `snapshot` er forståelig, eller bør kommandoene få
   andre navn i en senere hovedversjon?

## Foreløpig anbefaling

Inntil punktene over er avgjort, er anbefalt retning:

- separat kommando og separat repositoryformat fra dagens mirror
- eksakt repositorymappe som `PLASSERING`, uavhengig av samlingsnavnet
- initialisering bare av manglende eller helt tom mappe, uten `--adopt` i
  første versjon
- Bildebank-eid repositoryformat uten ekstern backupmotor
- `format_version: 1` i repositorymetadata og manifest, med avvisning av
  ukjente påkrevde egenskaper og uten omskriving av publiserte snapshots
- nøyaktig én `collection_id` per repository
- ukomprimerte objekter i første formatversjon
- ingen Bildebank-kryptering i første formatversjon
- permanent bevaring og mulig gjenbruk av verifiserte, urefererte objekter
- read-only rapportering av repositorykorrupsjon uten reparasjonsfunksjon i
  første versjon
- avbrudd ved manglende eller åpenbart ugyldig gjenbrukt objekt, med advarsel
  om mulig medieskade
- eksklusiv repositorylås for alle snapshotoperasjoner i første versjon
- read-only, ulåst og rask `create --dry-run` som estimerer uten full hashing
- repository bare på lokal eller ekstern disk i første versjon; nettverksmål
  avvises
- støtte for FAT32 når alle konkrete filer er innenfor per-fil-grensen
- faste exitkoder og tydelige launcherresultater for `complete`, `degraded`,
  `recovery` og `failed`
- hel restore bevarer opprinnelig `collection_id` og advarer mot parallell bruk
  av original og gjenopprettet kopi
- valgfri, uforanderlig snapshotkommentar uten innvirkning på snapshot-ID eller
  katalognavn
- `snapshot create` i både CLI og launcher med felles underliggende kode
- `snapshot check --full` i både CLI og launcher med felles read-only kode
- ingen lagret kontrollhistorikk eller tidsbaserte kontrollpåminnelser i første
  versjon
- tekstbekreftelse før reell hel restore og restore av enkeltfil, med `--yes`
  som eksplisitt unntak
- konservativ, stagingbasert oppretting og full stivalidering av målmappe ved
  hel restore, uten automatisk sletting av avbrutte mediefiler
- enkeltfil-restore uten overskriving, med relativ eksportsti, eksplisitt
  variantvalg, stabil `entry_id` for redningsposter og tydelig navngiving av
  observert variant
- ingen innebygd tidsplanlegging i første versjon
- snapshotliste, problemliste og restore som CLI-only i første launcherutgave
- `manifest.json`, strømbar `files.jsonl` og kontrollerende `commit.json` som
  sannhetskilde uten krav om en repositorydatabase
- rå, ukomprimerte objekter og `README.txt` som muliggjør teknisk manuell
  gjenoppretting; direkte mappeblaing og snapshot-browser er ikke krav i første
  versjon
- kontroll av sist bekreftede maskin og samlingssti, med eksplisitt bekreftelse
  når samme logiske samling er flyttet
- append-only, innholdsadressert lagring uten automatisk sletting
- alle vanlige filer i samlingsmappen, med bare den eksplisitte
  eksklusjonslisten
- SHA-256-verifisering mot `files` for både aktive og slettede filer
- størrelseskontroll, men ikke ny hashing, av gjenbrukte backupobjekter ved
  vanlig snapshot; periodisk full hashing med `snapshot check --full`
- publisering av `degraded` snapshot med avvik per filpost og bevaring av
  observerte filvarianter
- ett nytt forsøk for ukjente ustabile filer, der fortsatt feil gir `degraded`
  uten gyldig objektreferanse
- publisering av `recovery`-snapshot ved bekreftet feil i hoveddatabasen, uten
  å tillate vanlig hel restore
- recovery etter enhver bekreftet feil i hoveddatabasen bare mot et allerede
  bundet repository på sist bekreftede arbeidssted
- `degraded` ved feil i tilleggsdatabase, med rå databasefiler bevart for
  undersøkelse
- eksplisitt databasekatalog for hoveddatabase, OpenCLIP, alle face-modeller og
  andre SQLite-databaser, også med trygg policy for absolutt face-databasesti
- SQLite backup-API og target-lås for konsistente snapshots, uten at aktive
  database- eller sidefiler samtidig behandles som vanlige restorefiler
- portabel Windows-sikker stikontrakt og `recovery_only`-bevaring av lesbare
  filer som ikke kan få en normal restore-sti
- read-only forhåndskontroll som oppdager og avviser alle lenker og reparse
  points før repositorymetadata eller backupdata skrives
- flush og atomisk publisert snapshotmappe med gyldig `commit.json` som eneste
  definisjon av et `complete`, `degraded` eller `recovery` snapshot
- read-only kontroll før restorefunksjonen regnes som ferdig
- restore til ny mappe, aldri automatisk overskriving av aktiv samling

Planen er godkjent som grunnlag for implementeringen. Implementeringsstatusen
over skal holdes oppdatert til alle trinn er ferdige og verifisert.

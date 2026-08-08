# Versjonerte snapshots

Status: implementert. Dette dokumentet beskriver de varige kontraktene som må
bevares ved feilretting og videreutvikling. Brukerflyt og kommandoeksempler står
i `docs/snapshot.md`; gjennomført Windows-validering står i
`devel-docs/versionert-backup-windows-pilot.md`.

## Sikkerhetsmodell

Snapshot-repositoryet er append-only og innholdsadressert:

- Et publisert snapshot, og objektene det trenger, skal aldri overskrives,
  omskrives eller slettes av Bildebank.
- Verifiserte objekter som ikke refereres av et publisert snapshot, skal også
  beholdes. Det finnes ingen prune- eller cleanup-flyt som sletter objekter.
- Et objekt publiseres først etter kontroll av størrelse og SHA-256. Et
  snapshot publiseres først når `commit.json` kontrollerer både manifestet og
  fillisten.
- Avbrudd, full disk eller strømbrudd skal ikke skade eldre snapshots. Nye
  objekter og metadata bygges på samme filsystem i staging og publiseres med
  rename etter flush.
- Korrupsjon i repositoryet skal bare rapporteres. Bildebank skal ikke reparere,
  overskrive eller sette objekter i karantene automatisk.
- SHA-256 oppdager tilfeldig skade, men er ikke en signatur. En angriper med
  skrivetilgang kan endre både data og kontrollsummer. Flere medier, en
  frakoblet kopi og en kopi utenfor boligen er fortsatt nødvendig.

Repositoryet inneholder ukomprimerte og ukrypterte originalbyte. Formatet skal
kunne reddes manuelt ved hjelp av `README.txt`, JSON/JSONL-manifestene og
objektlageret. Implementasjonen skal ikke avhenge av hardlinks, reflinks eller
filsystemsnapshots.

## Eierskap og kodekart

- `snapshot.py`: planlegging, inventar, eksklusjoner, repository- og
  stivalidering.
- `snapshot_create.py`: låser, binding/initialisering, normal- og recovery-flyt
  og publisering.
- `snapshot_builder.py`: matching mot hoveddatabasen, databasekatalog og
  bygging av fil- og databaseposter.
- `snapshot_repository.py`: format-v1, objektlagring, SQLite-backup,
  kanonisk serialisering og atomisk publisering.
- `snapshot_check.py`: parsing, rask/full kontroll og snapshotliste.
- `snapshot_restore.py`: problemliste, restoreplaner og konservativ
  publisering av hel samling eller enkeltfil.
- `launcher_snapshot_tab.py`: vindusgrensesnitt over de samme plan-, create-,
  check- og enkeltfilfunksjonene. Launcheren skal ikke ha et eget format.

Publisert repositoryinnhold er sannhetskilden. Lokal launcher-/dashboardstatus
i `.bildebank-program.sqlite3` er bare en regenererbar hjelpeindeks med blant
annet repository-ID, collection-ID, sti, snapshot-ID, status og tidspunkt. Feil
ved lokal registrering skal aldri endre resultatet av et publisert snapshot.

## `files` og `file_sources`

I gjeldende hovedschema (v23 da dette ble skrevet) er `files` den kanoniske
raden for en fysisk fil i samlingen. Snapshot leser filen fra samlingsroten via
`files.target_path` og kontrollerer den mot `files.sha256` og
`files.size_bytes`. Dette gjelder også rader som peker under `deleted/`.

`file_sources` er provenienshistorikk. Hver rad peker til `files.id` med
`file_id`; flere kildeposter kan peke til samme kanoniske fil. Snapshot skal
aldri hente innhold fra `file_sources.source_path`, fordi importkilden kan være
frakoblet eller borte. Proveniensen bevares gjennom kopien av hoveddatabasen.

## Repository og binding

`PLASSERING` er den eksakte repositorymappen. Ett repository tilhører nøyaktig
én logisk samling og har én stabil `repository_id` og samlingens
`collection_id`.

```text
repository/
  .bildebank-backup-repository.json
  .bildebank-repository.lock       # finnes bare mens en operasjon kjører
  README.txt
  objects/
    sha256/ab/cd/<sha256>-<size_bytes>
  snapshots/
    <UTC-tid>-<snapshot_id>/
      manifest.json
      files.jsonl
      commit.json
  incomplete/
    <run_id>/
```

En manglende repositorymappe kan opprettes når forelderen finnes, og en helt
tom mappe kan initialiseres. En ikke-tom mappe uten gyldige metadata avvises
urørt. Det finnes ingen `--adopt`. Repositoryet kan ikke ligge i eller over
samlingen, inne i et annet repository eller på UNC/SMB/NAS. Lokale og eksterne
disker støttes; FAT32 støttes når hver konkret fil er innenfor filsystemets
per-fil-grense.

Metadata lagrer sist bekreftede maskin og absolutte samlingssti. Endret maskin
eller sti avbryter opprettingen til brukeren uttrykkelig bekrefter at den samme
logiske samlingen er flyttet. Bekreftelsen kan ikke brukes hvis hoveddatabasen
er skadet. En kopiert samling beholder samme `collection_id`; original og kopi
må derfor ikke utvikles uavhengig mot samme repository.

Et klonet repository beholder samme `repository_id`. Originalen og klonen må
ikke brukes videre som to uavhengige skrivbare repositories. Separate
backupmedier i rotasjon skal initialiseres separat og får hver sin
`repository_id`.

## Innhold og databasekopier

Hele samlingstreet inventeres uten å følge lenker. Alle vanlige filer tas med,
også ukjente filer og migreringsbackuper, med disse eksplisitte unntakene i
roten:

- mappene `browser/`, `thumbs/` og `video-previews/`
- `.bildebank.lock` og `.bildebank.log`
- `index.html`, `image-search.html`, `personer.html` og `person-*.html`

De samme filnavnene under andre mapper er ikke automatisk utelatt. `deleted/`
skal alltid være med. Foreldreløse SQLite-sidefiler tas med som vanlige ukjente
filer; `-wal`, `-shm` og `-journal` tas ut av normalinventaret bare når de
tilhører en katalogført database.

Databasekatalogen har disse rollene:

- `main`: `.bilder.sqlite3`, nødvendig for normal hel restore.
- `openclip`: `.bilder-openclip.sqlite3`, regenererbar, men tas med.
- `face:<model_name>`: alle kontrollerte databaser under
  `.bildebank-faces/`; kuraterte persondata gjør dem ikke-regenererbare.
- `auxiliary:<relative-path>`: andre SQLite-databaser i samlingen.

En frisk SQLite-database kopieres konsistent med SQLite backup-API og
integritetskontrolleres før den lagres som objekt. Den åpne databasefilen og
sidefilene skal ikke samtidig få en annen restorebetydning i filinventaret.
Alle databasene beskyttes mot Bildebank-skriving av samlingens `TargetLock`.

Eldre format-v1-snapshots kan ha face-databaser fra en absolutt
`database_dir`. De skal fortsatt kunne leses, men restore skriver dem under
`.bildebank-faces/` og endrer ikke brukerens konfigurasjon automatisk.

## Nøyaktig format-v1

`format_version` er heltallet `1`, og `required_features` er tom i v1. En
skriver skal avvise ukjent formatversjon eller ukjent påkrevd egenskap.
Publiserte snapshots migreres eller omskrives aldri. Ukjente valgfrie felt kan
ignoreres ved lesing; de må bevares dersom den muterbare repositorymetadataen
senere skrives om. Endringer som påvirker nødvendig lese- eller skriveatferd,
krever ny formatversjon eller en eksplisitt påkrevd egenskap.

### Kanoniske verdier

- JSON og JSONL er UTF-8 uten BOM, med sorterte objektnøkler, ingen unødvendige
  mellomrom og LF. Vanlige JSON-filer har én avsluttende LF.
- `files.jsonl` har én kanonisk JSON-post per linje og kan strømles. Tom
  filliste er en tom fil.
- SHA-256 er 64 små hex-tegn. Store heltall som objektstørrelse, `mtime_ns` og
  postantall lagres som desimalstrenger uten fortegn eller ledende nuller.
- Tider er UTC som `YYYY-MM-DDTHH:MM:SSZ`. ID-er er kanoniske UUID-er med små
  bokstaver og bindestreker.
- Snapshotmappen heter `YYYY-MM-DDTHHMMSSZ-<snapshot_id>`, basert på
  `completed_at`.

En objektreferanse er:

```json
{"algorithm":"sha256","sha256":"<64 små hex-tegn>","size_bytes":"123"}
```

Den eneste fysiske stien for referansen er:

```text
objects/sha256/<hash[0:2]>/<hash[2:4]>/<hash>-<size_bytes>
```

Hash og størrelse utgjør sammen objektnøkkelen. Objektet må være en vanlig fil
med angitt størrelse.

### Repositorymetadata

`.bildebank-backup-repository.json` krever:

- `format_version`, `required_features`, `repository_id` og `collection_id`
- `collection_name`, `created_at` og `created_by {program, version}`
- `last_confirmed_source {collection_path, machine_name, confirmed_at}`

Bare denne metadatafilen er muterbar. Oppdatering skal bruke tempfil, flush og
atomisk replace og må aldri endre repository-/collection-ID eller publiserte
snapshots.

### `manifest.json`

Manifestet krever:

- identitet: `format_version`, `required_features`, `snapshot_id`,
  `repository_id`, `collection_id` og `collection_identity`
- tid/opphav: `started_at`, `completed_at`, `created_by` og valgfri `note`
- innhold: `databases`, `schema_versions`, `files_jsonl`, `exclusions` og
  `warnings`
- `status`: `complete`, `degraded` eller `recovery`

`note` er `null` eller maksimalt 1000 Unicode-tegn uten kontrolltegn.
`files_jsonl` inneholder `entry_count`, `sha256` og `size_bytes` for hele
fillisten.

`collection_identity` er eksakt
`{"source":"database","verified":true}` for `complete` og `degraded`, og
`{"source":"repository","verified":false}` for `recovery`.

Hver databasepost krever:

- `role`, `source_path_display`, `restore_path`, `required`, `regenerable`
- `capture`: `sqlite_backup` eller `raw_recovery`
- `status`: `ok`, `backup_failed` eller `unreadable`
- `object`, `schema_version` og `model_name`

En `sqlite_backup` må ha status `ok`, objekt og portabel `restore_path`.
`raw_recovery` kan ikke ha normal restore-sti; råfilene representeres som
`recovery_only`-poster. `source_path_display` er bare visningstekst og må aldri
brukes som restoremål. Det skal finnes nøyaktig én `main`-post. `complete` og
`degraded` krever konsistent hoveddatabase; `recovery` krever `main` som
`raw_recovery`.

### `files.jsonl`

Hver post krever:

- `entry_id`, `path`, `original_path_display`, `recovery_name`
- `record_type`: `file` eller `database_raw`
- `restore_kind`: `normal` eller `recovery_only`
- `integrity_status`: `ok`, `missing`, `unreadable`, `hash_mismatch`,
  `size_mismatch`, `changed_during_snapshot`, `unsafe_path` eller
  `database_backup_failed`
- `expected`, `object` og `mtime_ns`

Normale poster sorteres etter `path`; deretter følger `recovery_only` sortert
etter `original_path_display`. `entry_id` er stabilt innen snapshotet og er
`e-` pluss det tolv-sifrede linjenummeret. Manglende, duplisert eller ustabil
ID gjør snapshotet ugyldig.

En normal post har portabel `path` og `recovery_name: null`. En
`recovery_only`-post har `path: null` og det programgenererte navnet
`entry-<tolv sifre>.bin`. `original_path_display` er aldri en sikker målsti.

For databaseførte filer er `expected` den registrerte SHA-256 og størrelsen.
`object` peker til observerte, lagrede byte eller er `null` når ingen stabil
bytefølge kunne sikres. Ukjente filer har `expected: null`. `mtime_ns` er
opprinnelig filendringstid eller `null`.

### `commit.json` og publisering

`commit.json` har eksakt feltene `format_version`, `snapshot_id`, `manifest`
og `files_jsonl`; de to siste inneholder SHA-256 og størrelse. Filen skrives
sist. Et snapshot er publisert bare når den endelige snapshotmappen finnes og
har en gyldig `commit.json` som stemmer med de to andre filene.

Ufullstendige kjøringer blir liggende under `incomplete/<run_id>/`. De skal
aldri fortsettes, endres eller slettes automatisk. Neste create starter med ny
run-ID og kan bare gjenbruke ferdig publiserte objekter. `snapshot check`
rapporterer ufullstendige kjøringer og urefererte objekter.

## Portable stier

En normal sti er relativ UTF-8 med `/`. Den kan ikke være tom eller absolutt,
ha tomme komponenter, `.`/`..`, bakstrek, kolon, kontrolltegn eller Windows-
tegnene `<`, `>`, `"`, `|`, `?`, `*`. Komponenter kan ikke ende i punktum eller
mellomrom eller være Windows-reserverte navn (`CON`, `PRN`, `AUX`, `NUL`,
`COM1`–`COM9`, `LPT1`–`LPT9`), heller ikke med filendelse.

Alle stier får en Windows-sikker, case-insensitiv kollisjonsnøkkel. Lesbare
filer med ikke-portabel sti eller kollisjon bevares som `recovery_only`, og
snapshotet blir `degraded`. Databaseførte utrygge stier skal aldri følges
utenfor samlingen.

Symbolske lenker, junctions, andre reparse points og andre filtyper enn vanlige
filer og mapper avvises. Kontrollen skjer både i et read-only inventar og ved
den faktiske åpningen, uten å følge lenker.

Restore validerer alle stier og kollisjoner på nytt før første fil skrives.
Dette er nødvendig fordi repositoryet kan være skadet eller manipulert.

## Oppretting og integritet

`snapshot create --dry-run` er rask og helt read-only: ingen full hashing,
mapper, metadata, staging eller låsfiler. Den validerer kilde/repository,
inventar, filtilstedeværelse og størrelse, portable stier, anslått objektbehov,
ledig plass og per-fil-grense. Endelige hashavvik og kopitall er derfor ukjent
til reell kjøring.

Reell create tar repositorylåsen før samlingens `TargetLock` og slipper dem i
motsatt rekkefølge. Repositoryoperasjonene `create`, `check`, `list`,
`problems`, `restore` og `restore-file` bruker samme eksklusive repositorylås.
En stale lås etter krasj fjernes aldri automatisk.

For hver `files`-rad kontrolleres trygg plassering, vanlig filtype, størrelse
og SHA-256. Alle databaseførte mediefiler hashes på nytt ved hver reelle
kjøring, også når størrelse og mtime er uendret. Et gjenbrukt repositoryobjekt
kontrolleres normalt bare på filtype og størrelse; stille korrupsjon med samme
størrelse oppdages av `snapshot check --full`.

- Et avvik i én kildefil gir en publisert `degraded`-post med forventede og
  observerte verdier. Observerte byte bevares som eget objekt når de kan leses.
- En manglende eller uleselig fil får ingen observert objektreferanse, men
  resten av snapshotet kan fortsatt publiseres som `degraded`.
- En stabil, ukjent vanlig fil tas med og gjør ikke alene snapshotet degraded.
  Ved endring/lesefeil prøves filen én gang til; fortsatt feil gir `degraded`
  uten at ustabile byte presenteres som gyldig objekt.
- Et manglende eller ikke-regulært gjenbrukt objekt, eller et objekt med feil
  størrelse, er skade i repositoryet. Opprettingen avbrytes uten publisering;
  dette er ikke et kildeavvik som kan merkes `degraded`.
- Nye objekter hashverifiseres etter kopiering. Eksisterende objekt med samme
  nøkkel overskrives aldri.

Ved bekreftet feil i hoveddatabasen kan et `recovery`-snapshot bare opprettes
mot et allerede initialisert repository på sist bekreftede maskin og sti. Det
bruker repositoryets tidligere verifiserte `collection_id`; en lesbar ID fra
den skadede databasen må stemme, men gjør ikke identiteten verifisert. Nytt
repository, flyttebekreftelse eller vanlig hel restore er ikke tillatt i denne
modusen. Lesbare vanlige filer og rå database-/sidefiler bevares.

Feil i en tilleggsdatabase gir `degraded`, mens den friske hoveddatabasen
sikres normalt og rå tilleggsdatabasefiler bevares som `recovery_only`. Feil i
staging eller backupmålet skal derimot stoppe kjøringen uten publisering og må
ikke feilmerkes som kildekorrupsjon.

Create-resultater og CLI-exitkoder er:

- `complete`: publisert, exit `0`
- `degraded`: publisert med avvik, exit `3`
- `recovery`: publisert redningssnapshot, exit `4`
- `failed`: ikke publisert, exit `1`
- syntaks-/argumentfeil: exit `2`

Launcheren bruker samme interne resultatmodell og skal aldri tolke CLI-tekst.

## Kontroll

Rask `snapshot check` validerer repositorystruktur, format, `commit.json`,
manifest/filliste, objektnavn, størrelser, referanser og den konsistente
hoveddatabasekopien mot filpostene. Full kontroll hasher alle objekter på nytt,
også urefererte objekter.

Kontrollen er read-only bortsett fra den midlertidige låsfilen. Den endrer ikke
publisert status og lagrer ikke kontrollhistorikk. `complete`, `degraded` og
`recovery` beskriver tilstanden ved publisering; check rapporterer nåværende
gjenopprettbarhet og kobler skadde objekter til alle berørte snapshot-ID-er og
logiske stier.

## Restore

All restore starter med låst, read-only validering av metadata, commit,
manifest, objekter, ledig plass og hele settet av utstier. Dry-run skriver
ingenting. Ingen restore skal skrive inn i aktiv samling eller repository, og
ingen eksisterende fil overskrives.

Hel restore:

- avviser `recovery`-snapshots, ikke-tom eller innkapslet målmappe og rester
  etter tidligere restoreforsøk
- bygges i en unik søsken-stagingmappe på samme filsystem
- kopierer og SHA-256-verifiserer alle filer og databaser, gjenoppretter
  `mtime_ns` så langt filsystemet støtter, og kontrollerer hoveddatabase og
  opprinnelig `collection_id`
- gjenoppretter forventet databaseført variant til ordinær plass; observert
  avviksvariant går til en separat recovery-søstermappe
- lar ordinær fil mangle hvis forventet objekt ikke finnes, publiserer den
  bevisst ufullstendige samlingen og returnerer exit `3`
- publiserer eventuell recovery-mappe først og samlingsmappen sist; den siste
  rename-operasjonen definerer en publisert hel restore

Avbrutt staging og publiserte recoveryrester med mediefiler slettes eller
overskrives aldri automatisk. Restore av en samling bevarer `collection_id` og
advarer mot å bruke original og gjenopprettet kopi parallelt.

Enkeltfil-restore krever snapshot og enten normal `path` eller `entry_id`.
Når forventet og observert variant begge finnes, må varianten velges
eksplisitt. Observert variant får hash-suffiks. `recovery_only` kan bare velges
med `entry_id` og eksporteres under det programgenererte recovery-navnet.
Eksporten bevarer relativ sti og mtime, opprettes eksklusivt og verifiseres
under og etter kopiering.

Reell hel restore krever eksakt tekstbekreftelse; enkeltfil krever `j/N`.
`--yes` er det eksplisitte automatiseringsunntaket. Launcheren tilbyr create,
full check og konservativ eksport av én vanlig mediefil; hel restore og
`recovery_only`-eksport forblir CLI-funksjoner.

## Begrensninger

Format-v1 har ingen komprimering, Bildebank-kryptering, skylagring,
nettverksrepository, automatisk tidsplanlegging, prune, repository-reparasjon
eller snapshot-browser. Restore lover ikke å bevare Windows-opprettelsestid,
ACL/eierdata eller katalogtider. Slike egenskaper må vurderes som eksplisitte
format- og sikkerhetsendringer, ikke legges til som skjult sideatferd.

## Ved senere endringer

1. Les `app-design.md`, `docs/snapshot.md` og denne kontrakten.
2. Ikke endre format-v1 eller publiserte snapshots på stedet. Nye normative
   felt eller objektkoding krever kompatibilitetsbeslutning og vanligvis ny
   formatversjon.
3. Bevar låserekkefølge, staging, flush, atomisk publisering og regelen om
   aldri å slette eller overskrive mediefiler/repositoryobjekter.
4. Vurder alle endringer mot aktive filer, `deleted/`, ukjente filer,
   hoveddatabase, OpenCLIP, alle face-modeller og rå recoveryfiler.
5. Test både frisk kilde, kildeavvik, repositoryskade, målfeil, avbrudd og
   restore. Test Windows-stier, case-kollisjoner, reparse points og relevante
   eksterne filsystemer.
6. Hold CLI og launcher på felles plan-, create-, check- og restorekode.

Fokuserte tester:

```text
python -m pytest tests/test_snapshot_repository.py
python -m pytest tests/test_snapshot_builder.py
python -m pytest tests/test_snapshot_create.py
python -m pytest tests/test_snapshot_check.py
python -m pytest tests/test_snapshot_restore.py
python -m pytest tests/test_snapshot_cli.py
python -m pytest tests/test_launcher_snapshot_tab.py
```

Ved endringer i format, atomisitet eller filsystematferd må også de relevante
Windows-testene og en faktisk restoreøvelse kjøres før funksjonen regnes som
trygg.

# Implementasjonsplan: permanent sletting og tombstones

Status: Kontrollpunkt A er implementert og verifisert. Kontrollpunkt B og C
er ikke startet.

Denne planen beskriver rekkefølgen for å implementere designet i
`devel-docs/sletting-med-toombstone.md`. Designdokumentet er autoritativt for
produktatferd og sikkerhetsregler. Denne planen skal ikke brukes til å endre
allerede avklarte valg. Hvis implementasjonen avdekker en reell konflikt med
designet, stoppes arbeidet og valget avklares før koden fortsetter.

## 1. Mål og avgrensning

Første leveranse skal gi:

- database v21 med `file_tombstones`, `pending_file_purges` og nødvendige
  integritetsregler
- tombstone-kontroll ved import
- sikker permanent sletting av én fil og tømming av papirkurven
- recovery, retry og avbryting av journalførte purger
- tombstone-liste og eksplisitt fjerning av én tombstone
- nødvendige sperrer i andre filoperasjoner
- `doctor`-kontroller, logging, Web-UI og dokumentasjon

Følgende er ikke del av første leveranse:

- sletting eller endring av eksisterende snapshots, sidecar-backuper eller
  andre sikkerhetskopier
- en brukerrettet CLI-kommando for permanent sletting
- automatisk opprettelse av tombstones for manglende filer
- rekursiv sletting av innhold under `deleted/`, `thumbs` eller
  `video-previews`
- ny opprydding av InsightFace- og OpenCLIP-data ved purge; disse dataene
  slettes allerede av `remove`, og historiske rester ble ryddet i v17
- brede refaktoreringer av import, server eller fillivsløp

Når implementasjonen starter, må prosjektregelen som sier at bare `unimport`
kan slette bildefiler permanent, oppdateres med et smalt unntak for den nye,
bekreftede purge-flyten. De øvrige sikkerhetsreglene beholdes.

## 2. Eksisterende byggesteiner som skal gjenbrukes

Implementasjonen skal bygge på dagens mekanismer:

- `bildebank/file_lifecycle.py` for mønsteret rundt target-lås,
  filidentitet, journalføring og recovery
- `bildebank/file_moves.py` for eksisterende oppstartsrecovery
- `bildebank/collection_paths.py` for sikre samlingsstier, lenker, Windows
  reparse points og stabil hashing
- `bildebank/derived_files.py` for avledning og klassifisering av thumbnails
  og videoavspillingskopier
- `bildebank/pending_deletes.py` for lavnivåmønsteret rundt kontrollert
  unlink og best-effort opprydding av tomme mapper
- `bildebank/target_lock.py` for serialisering av samlingsendringer
- `bildebank/server_actions.py`, `server_endpoints_items.py`,
  `server_handler.py` og `server_app.py` for dagens Web-UI-flyt

`pending_file_purges` og `pending_file_deletes` skal ikke dele
livsløpslogikk. Felles, små funksjoner for stikontroll, stabil identitet og
unlink kan trekkes ut eller gjenbrukes når det kan gjøres uten en bred
refaktorering.

## 3. Leveranserekkefølge og kontrollpunkter

| Kontrollpunkt | Innhold | Brukersynlig atferd | Windows-test |
| --- | --- | --- | --- |
| A | Database v21 og datatilgang | Ingen ny slettefunksjon | Migrering og full testsuite |
| B | Purge-motor, recovery, import, sperrer og `doctor` | Ingen knapper ennå | Fokusert test av låst fil, deretter full testsuite |
| C | Web-UI, tombstone-administrasjon og dokumentasjon | Hele funksjonen | End-to-end i disponibel testsamling |

Hvert kontrollpunkt skal:

- være internt konsistent og ha grønn testsuite
- kunne committes separat av brukeren
- ha en kort, konkret commit-melding
- ikke aktivere en uferdig destruktiv brukerflyt

## 4. Forberedelse før kode

- [x] Commit designdokumentet separat fra implementasjonen.
- [x] Kontroller at arbeidskopien ikke har overlappende, urelaterte endringer.
- [x] Kjør full Linux-testsuite som baseline:
  `python -m pytest -n auto`.
- [x] Kjør prosjektets lint/formatkontroll uten å formatere urelaterte filer.
- [x] Oppdater den permanente-slettingsregelen først når implementasjonen
  faktisk begynner.

## 5. Kontrollpunkt A: database v21

### 5.1. Skjema

Øk `SCHEMA_VERSION` fra 20 til 21 i `bildebank/db_schema.py`.

Opprett `file_tombstones` med minst:

- `id INTEGER PRIMARY KEY`
- `sha256 TEXT NOT NULL UNIQUE`
- `size_bytes INTEGER NOT NULL`
- `original_filename TEXT NOT NULL`
- `former_target_path TEXT NOT NULL`
- `purged_at TEXT NOT NULL`

Opprett en indeks som støtter stabil visning etter `purged_at` og `id`.

Opprett `pending_file_purges` med minst:

- `id INTEGER PRIMARY KEY`
- `file_id INTEGER NOT NULL UNIQUE`
- `expected_path TEXT NOT NULL`
- `expected_sha256 TEXT NOT NULL`
- `expected_size_bytes INTEGER NOT NULL`
- `expected_deleted_at TEXT NOT NULL`
- `original_filename TEXT NOT NULL`
- `former_target_path TEXT NOT NULL`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `last_error TEXT`
- `created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP`
- `updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP`

`file_id` skal referere til `files(id)` med `ON DELETE RESTRICT` eller
tilsvarende `NO ACTION`, aldri `CASCADE`.

Legg til triggere som avviser:

- `INSERT` eller endring av `files.sha256` når SHA-256 allerede finnes i
  `file_tombstones`
- `INSERT` eller endring av `file_tombstones.sha256` når SHA-256 allerede
  finnes i `files`

Triggerne er siste sikkerhetsbarriere. Applikasjonskoden skal fortsatt gjøre
egne kontroller for å kunne gi forståelige feil.

### 5.2. Migrering

Migrering fra v20 til v21 skal:

- kjøre i én databasetransaksjon
- bare opprette tabeller, indekser og triggere
- opprette tomme tombstone- og purge-tabeller
- ikke lese eller endre bildefiler
- ikke endre eksisterende `files`- eller `file_sources`-rader
- ikke opprette tombstones fra `deleted/` eller fra manglende filer

Nye databaser skal opprettes direkte med samme v21-skjema.

Oppdater:

- `MigrationPlan` og CLI-teksten for `migrate --check` og `migrate`
- valideringen av gjeldende skjema
- eksempel-/testdatabaser som inngår i repositoryets vanlige
  migreringsprosedyre
- `devel-docs/database-v21-migration.md`

### 5.3. Datatilgang

Legg små, målrettede databasefunksjoner i eksisterende `db_*`-moduler, og
eksporter dem gjennom `bildebank/db.py`:

- hente og liste tombstones
- finne tombstone etter SHA-256
- liste og hente pending purger
- opprette og oppdatere purge-post
- hente eksakt purge-/filidentitet
- fjerne purge-post ved avbryting
- fullføre overgangen fra `files` til tombstone i én transaksjon

Fullføringstransaksjonen skal:

1. ha all nødvendig purge-informasjon lastet
2. fjerne purge-posten
3. fjerne `file_sources` og andre rader som ikke slettes automatisk
4. fjerne `files`-raden og rader med korrekt `ON DELETE CASCADE`
5. opprette tombstonen med `purged_at` satt til fullføringstidspunktet

Feil i et trinn skal rulle tilbake hele transaksjonen, inkludert den
restriktive purge-sperren.

### 5.4. Automatiske tester for kontrollpunkt A

Utvid primært `tests/test_migrate_cli.py` og legg til fokuserte
skjematester:

- v20 til v21 oppretter tomme tabeller, indekser og triggere
- ny database får v21 direkte
- `migrate --check` endrer ingenting
- migrering leser eller sletter ingen samlingsfiler
- samme SHA-256 kan ikke settes inn i begge tabeller
- både `INSERT` og endring av SHA-256 beskyttes
- `files` kan ikke slettes mens en purge-post finnes
- rollback gjenoppretter purge- og `files`-rad dersom tombstone-innsetting
  feiler
- eldre migreringsbaner ender i samme v21-skjema
- utviklerens schema-summary- og eksempelbasetester forventer v21

Kontrollpunkt A er ferdig når alle testene er grønne, men ingen kode kan ennå
starte permanent fysisk sletting.

## 6. Kontrollpunkt B: intern purge-motor

### 6.1. Ny, avgrenset domenemodul

Opprett en modul, for eksempel `bildebank/file_purge.py`, med dataklasser for:

- bekreftelsesidentitet:
  `file_id`, SHA-256, størrelse, forventet sti og `deleted_at`
- forhåndsvisning av én eller flere kandidater
- resultat per fil: slettet, ventende, hoppet over eller integritetsfeil
- samlet resultat for tømming av papirkurven

Hold HTTP, HTML og dialogtekst utenfor kjernemodulen.

### 6.2. Read-only forhåndsvisning

Implementer en funksjon som:

- bare velger databaseførte filer med `deleted_at`
- krever gyldig sti under `deleted/`
- klassifiserer nye kandidater og eksisterende purge-poster separat
- returnerer eksakt identitetsøyeblikksbilde, antall og samlet størrelse
- aldri oppretter purge-post eller endrer filsystemet
- ikke tar med ukjente filer under `deleted/`

Forhåndsvisningen skal kunne bygges både for én `file_id` og for hele
papirkurven.

### 6.3. Start av ny purge

Etter bekreftelse skal kjernemodulen:

1. ta target-låsen
2. kontrollere hele bekreftelsesidentiteten på nytt
3. kontrollere alle stikomponenter, vanlig fil, størrelse og SHA-256
4. hoppe over nye eller endrede kandidater uten å utvide utvalget
5. opprette én purge-post per gyldig fil i en databasetransaksjon
6. committe journalen før fysisk sletting

En purge-post er beviset på en allerede bekreftet brukerhandling. Ingen
annen kodevei skal kunne opprette den.

Ved tømming av papirkurven skal den samme target-låsen holdes gjennom
revalidering av hele det bekreftede utvalget, journalføring, alle fysiske
slettingsforsøk og registrering av ferdig eller mislykket resultat. En feil
for én fil skal likevel ikke avbryte forsøkene for de andre validerte filene.

### 6.4. Fysisk sletting

For hver journalførte purge:

1. avled de eksakte, programstyrte filstiene for gjeldende thumbnail,
   videoavspillingskopi og en eventuell entydig eldre thumbnail
2. kontroller at hver eksisterende avledet fil og alle dens stikomponenter er
   trygge
3. hash hver eksisterende avledet fil stabilt og kontroller filidentiteten
   igjen umiddelbart før unlink
4. slett bare de forventede enkeltfilene; manglende avledede filer er i orden
5. stopp før originalen dersom en eksisterende avledet fil ikke kan
   valideres eller slettes
6. kontroller originalens størrelse, SHA-256 og filidentitet rett før unlink
7. slett originalen fra `deleted/`
8. gjennomfør slutt-transaksjonen som oppretter tombstonen
9. fjern tomme, kjente overordnede mapper som best-effort

Ikke bruk `pending_file_deletes` som purge-journal. Gjenbruk bare
lavnivåvalidering der kontraktene er identiske.

### 6.5. Feil, retry og avbryting

Ved feil før originalen er borte:

- behold `files`-raden, originalen og purge-posten
- oppdater `attempts`, `updated_at` og `last_error`
- tillat **Prøv igjen** og, etter ny validering, **Avbryt permanent
  sletting**

Avbryting skal:

- kreve target-lås
- kreve at originalen fortsatt finnes og matcher purge-identiteten
- fjerne bare purge-posten i en transaksjon
- la filen bli liggende i `deleted/`
- ikke gjenopprette allerede fjernede, regenererbare avledede filer

Hvis originalen er borte:

- tillat ikke avbryting eller `undelete`
- retry skal bare fullføre gjenstående avledet opprydding og
  slutt-transaksjonen

Hvis noe annet finnes på originalstien:

- slett ingenting
- opprett ingen tombstone
- behold purge-posten med tydelig feil for manuell avklaring

### 6.6. Recovery

Legg purge-recovery inn i samme kontrollerte oppstarts-/operasjonskjede som
dagens `pending_file_moves`, uten å blande tabellenes logikk.

Recovery skal:

- bare behandle eksisterende purge-poster
- ikke prøve fysisk sletting automatisk når riktig original fortsatt finnes
- fullføre automatisk når originalen allerede er borte
- beholde og rapportere posten når stien inneholder noe annet
- gjøre ingen endringer ved read-only-åpning
- bare sperre den berørte filen, ikke resten av samlingen

Snapshot-oppretting og skrivbar serverstart skal se en konsistent
recovery-tilstand under target-låsen. Et snapshot skal aldri utløse en ny
brukerintensjon, men kan ta med en gyldig, ufullført purge-post og en
original som fortsatt finnes.

### 6.7. Sperrer i andre operasjoner

Mens `pending_file_purges` finnes for en fil:

- `undelete`, `remove` og `unimport` skal nekte å endre filen eller dens
  relaterte rader
- import av samme SHA-256 skal rapportere ventende permanent sletting og ikke
  opprette en ny `file_sources`-rad
- bakgrunnsjobber og andre muterende handlinger skal hoppe over filen eller
  avvise handlingen tydelig

Sperrene skal implementeres nær eksisterende livsløpskontroller, ikke som
spredte UI-spesialtilfeller.

### 6.8. Import og tombstones

Utvid importens SHA-256-oppslag før en ny `files`-rad eller
`file_sources`-rad opprettes:

- lik SHA-256 og størrelse i `file_tombstones`: hopp over og tell som
  eksplisitt permanent slettet
- lik SHA-256 og ulik størrelse: hopp over som integritetskonflikt
- ikke opprett `file_sources` for tombstone-treff
- fortsett med andre filer ved integritetskonflikt
- marker importen som delvis gjennomført og gi CLI-en feilstatus

Importer som allerede treffer en slettet `files`-rad uten purge, beholder
dagens oppførsel.

### 6.9. `doctor`

Utvid `doctor` read-only med kontroller for:

- SHA-256 som finnes i både `files` og `file_tombstones`
- purge-post uten korrekt `files`-rad
- purge-identitet som ikke stemmer med `files`
- riktig original, manglende original og uventet innhold som separate
  tilstander
- manglende fil uten purge-post som ordinært filavvik, aldri som tombstone

`doctor` skal ikke opprette, fjerne eller fullføre purger og tombstones.
Eksisterende sidecar-kontroller skal fortsatt rapportere eventuelle
historiske sidecar-avvik; purge skal ikke duplisere sidecar-oppryddingen.

### 6.10. Logging

Bruk `command_log` for brukerhandlingene:

- permanent sletting av én fil
- tømming av papirkurven
- retry
- avbryting
- fjerning av tombstone

Logg type handling og aggregerte tellinger. Ikke legg fillister, filnavn,
stier eller SHA-256 i `command_log.args_json`. Automatisk recovery skal kunne
skilles fra en ny brukerhandling uten å skape en ny sletteintensjon.

### 6.11. Automatiske tester for kontrollpunkt B

Opprett en fokusert testfil, for eksempel `tests/test_file_purge.py`, og
utvid relevante eksisterende tester.

Kjernescenarier:

- vellykket purge fjerner original, aktuelle avledede filer og `files`-rad
  og oppretter tombstone
- purge sletter alle `file_sources` og hoveddatabasens relaterte rader
- ukjent fil under `deleted/` berøres aldri
- endret, manglende, låst eller lenket original starter ikke en ny purge
- utrygg eller låst avledet fil hindrer sletting av originalen
- manglende avledet fil er i orden
- tomme mapper ryddes uten rekursiv sletting og uten å blokkere tombstonen
- én feil i en bulkoperasjon hindrer ikke andre validerte filer
- bekreftet utvalg utvides aldri med nye filer
- gjenbrukt `file_id` eller endret identitet hoppes over

Journal- og krasjgrenser:

- feil før journal-commit endrer ingenting
- feil etter journal-commit, men før unlink, etterlater retry-/avbrytbar post
- feil etter sletting av én avledet fil beholder originalen
- feil etter original-unlink, men før slutt-commit, fullføres av recovery
- feil ved tombstone-innsetting ruller databaseendringene tilbake og lar
  recovery fullføre senere
- recovery sletter ikke en original som fortsatt finnes
- recovery fullfører når originalen er borte
- uventet innhold på stien beholdes

Integrasjoner:

- import hopper over tombstone uten `file_sources`
- integritetskonflikt påvirker ikke andre importfiler og gir feilstatus
- import til en vanlig slettet fil beholder dagens oppførsel
- `undelete`, `remove` og `unimport` blokkeres av pending purge
- avbryting gjør vanlig `undelete` tilgjengelig igjen
- `doctor` rapporterer uten å reparere
- snapshot og read-only-åpning håndterer pending purge uten å opprette
  brukerintensjon
- aktive sidecar-databaser er allerede ryddet etter `remove`

Bruk feilinjeksering/mocking for hvert krasjpunkt. Testene skal bruke
disponible samlinger og aldri en ekte bildesamling.

Kontrollpunkt B er ferdig når domenelogikken er fullstendig testet, men ingen
ufullstendig Web-UI-knapp er synlig.

## 7. Kontrollpunkt C: Web-UI og tombstone-administrasjon

### 7.1. Serverhandlinger og endepunkter

Legg tynne serverhandlinger over kjernemodulen for:

- forhåndsvisning av permanent sletting av én fil
- bekreftet permanent sletting av én fil
- forhåndsvisning av **Tøm papirkurven**
- bekreftet tømming av det eksakte utvalget
- retry av én purge
- avbryting av én purge
- listing av tombstones
- bekreftet fjerning av én tombstone

Alle muterende endepunkter skal:

- være utilgjengelige i read-only-modus
- bruke CSRF-beskyttet `POST`
- validere JSON-form og størrelsesgrenser gjennom eksisterende
  request-infrastruktur
- la kjernemodulen ta target-låsen og gjøre endelig identitetskontroll
- returnere korte, ufarlige feilmeldinger uten lokale absolutte stier eller
  databaseinterne detaljer

### 7.2. Bekreftelsesgrunnlag

Serveren skal bygge identitetsøyeblikksbildet:

- `file_id`
- SHA-256
- størrelse
- forventet relativ sti under `deleted/`
- `deleted_at`

Klienten skal aldri kunne gjøre utvalget bredere enn det serveren
forhåndsviste. Ved bekreftelse kontrolleres hele identiteten på nytt under
target-låsen. Avvik krever ny forhåndsvisning.

Tombstone-fjerning bindes tilsvarende til:

- tombstone-ID
- SHA-256
- størrelse
- `purged_at`

### 7.3. `/settings/removed`

Utvid siden med:

- **Slett permanent** ved siden av **Undelete** for ordinære papirkurvfiler
- **Tøm papirkurven** med antall og samlet størrelse i bekreftelsen
- tydelig markering av eksisterende purger som **Nytt forsøk**
- **Prøv igjen** og **Avbryt permanent sletting** når originalen finnes
- **Venter på å fullføre permanent sletting** og bare **Prøv igjen** når
  originalen allerede er borte
- resultatvisning som skiller slettede, ventende og hoppede filer
- dialogen **Enkelte filer kunne ikke slettes. [Lukk]** etter delvis
  bulkfeil

Bekreftelsen skal advare om:

- at handlingen er permanent i den aktive samlingen
- at eldre snapshots og andre sikkerhetskopier fortsatt kan inneholde
  filene

Tekniske recovery-detaljer skal ikke vises når en enkel status er nok.

### 7.4. Tombstone-liste

Vis tombstones på en tydelig del av `/settings/removed` eller en underordnet
settings-side med:

- ID
- opprinnelig filnavn
- tidligere plassering
- størrelse
- `purged_at`

Hver rad skal ha en eksplisitt handling for å fjerne tombstonen. Dialogen
skal forklare at ingen fil gjenopprettes, men at innholdet kan importeres
igjen fra enhver kilde etterpå.

### 7.5. Frontend

Utvid `bildebank/assets/server.js` og CSS uten å lage en parallell
frontendarkitektur:

- bruk eksisterende CSRF-hjelper
- deaktiver knapper mens en request pågår
- håndter retry-dialogen for enkeltfil
- oppdater eller last siden på nytt etter mutasjoner
- behold bare **Lukk** etter delvis bulkfeil
- unngå at dobbeltklikk oppretter flere forsøk

### 7.6. Automatiske tester for kontrollpunkt C

Utvid primært `tests/test_server_item_actions_cli.py` og
`tests/test_remove_undelete_cli.py`:

- riktige knapper og statuser på `/settings/removed`
- skrivehandlinger avvises i read-only-modus
- CSRF og bare-POST-kontrakten
- bekreftelsesidentitet sendes og revalideres
- gammel side, endret fil og gjenbrukt ID avvises
- enkeltfil: bekreft, suksess, feil, JA-retry og NEI
- bulk: eksakt utvalg, delvis suksess og bare **Lukk**
- eksisterende purger vises og gjenbrukes
- avbryting og påfølgende `undelete`
- manglende original viser bare fullførings-retry
- tombstone-listing og sikker tombstone-fjerning
- browser-/navigasjonscacher tømmes etter relevante endringer
- feilrespons lekker ikke lokal sti eller databaseinformasjon

Kontrollpunkt C er ferdig når hele brukerflyten er tilgjengelig og alle
tester og dokumentasjonskrav er oppfylt.

## 8. Dokumentasjon ved implementasjon

Oppdater samtidig med den fasen som endrer atferden:

- `app-design.md` med database v21 og den ferdige livsløpskontrakten
- `devel-docs/database-v21-migration.md`
- `docs/migrate.md`
- brukerhjelp for `/settings/removed`, permanent sletting, retry,
  avbryting og tombstone-fjerning
- eventuell dokumentasjon som fortsatt sier at bare `unimport` kan slette
  permanent

Brukerdokumentasjon skal bruke Windows-filnavn og forklare handlingene uten å
forutsette kjennskap til SQLite, SHA-256 eller transaksjoner.

## 9. Verifikasjon og brukerens kontrollpunkter

Codex kjører under hver fase:

- fokuserte tester uten `-n`
- full testsuite med `python -m pytest -n auto` ved hvert kontrollpunkt
- lint/formatkontroll
- `git diff --check`
- diff- og statuskontroll for å sikre at urelaterte filer ikke er endret

Brukeren trenger normalt bare å teste på Windows ved kontrollpunktene:

### Etter kontrollpunkt A

- migrer kopierte testdatabaser uten tombstones
- kontroller at migreringen fullføres og at eksisterende bilder og
  papirkurvinnhold er uendret
- kjør full Windows-testsuite

### Etter kontrollpunkt B

- kjør de fokuserte purge- og recovery-testene på Windows
- la Windows-testene bruke disponible testsamlinger og virkelige åpne
  filhåndtak for låst original og låst avledet fil
- opprett ingen midlertidig brukerrettet purge-kommando bare for dette
  kontrollpunktet
- kjør full Windows-testsuite

### Etter kontrollpunkt C

- test hele `/settings/removed`-flyten i en disponibel samling
- test enkeltfil, tømming, delvis feil, retry og avbryting
- kontroller tombstone-listen og fjern én tombstone
- reimporter samme kildefil før og etter tombstone-fjerning
- kontroller at snapshots og sikkerhetskopier ikke endres
- kjør full Windows-testsuite

Ingen manuell test skal utføres først mot brukerens primære bildesamling.

## 10. Ferdigkriterier

Implementasjonen er ferdig når:

- v21 migrerer alle støttede eldre schemaer og nye databaser bruker v21
- SHA-256 ikke kan finnes i både `files` og `file_tombstones`
- en vellykket purge alltid ender med borte original og ferdig tombstone
- en mislykket purge alltid beholder en entydig journal
- manglende filer uten brukerbekreftet purge aldri får tombstone
- import ikke gjeninnfører tombstonet filinnhold
- pending purger ikke kan endres av andre livsløpsoperasjoner
- recovery aldri starter en ny sletteintensjon
- bulkoperasjoner aldri utvider det bekreftede utvalget
- ukjente filer, snapshots og sikkerhetskopier aldri slettes
- Linux- og Windows-testsuitene er grønne
- dokumentasjon og prosjektregler beskriver den implementerte atferden

# Gruppering av bilder med OpenCLIP

Status: første leveranse implementert. Den manuelle verifikasjonen på Windows
med omtrent 20 000 embeddings gjenstår før funksjonen brukes på
hovedsamlingen.

Dette dokumentet beskriver den opprinnelige schema-v2-leveransen med
MiniBatchKMeans og HDBSCAN. Leiden-utvidelsen og gjeldende schema v3 beskrives
i `devel-docs/image-clustering2.md`.

## Mål

Bildebank skal kunne lage reversible forslag til grupper av visuelt eller
semantisk lignende bilder ved å bruke eksisterende OpenCLIP-embeddings.

En grupperingskjøring skal være avledet sidecar-data. Den skal ikke automatisk:

- endre tagger, personer, steder eller kommentarer
- flytte, slette eller skjule bildefiler
- opprette en permanent samling
- gjøre gruppen eller klyngenummeret til en egenskap ved et bilde

Brukeren skal kunne inspisere og slette en kjøring uten at bilder,
`file_sources`, metadata eller embeddings påvirkes. Senere kan UI-et tilby
eksplisitte, bekreftede handlinger som å opprette en samling eller legge en
tagg på medlemmene.

## Anbefalt første leveranse

Første leveranse bør være:

1. Start en kjøring fra **Verktøy**-fanen i launcheren.
2. Velg alle aktive stillbilder eller et utvalg uttrykt med dagens filtersøk.
3. Bruk gjeldende konfigurerte OpenCLIP-modell og allerede lagrede embeddings.
4. Gruppér med `MiniBatchKMeans` og et eksplisitt antall grupper.
5. Kjør beregningen som en avbrytbar underprosess gjennom launcherens
   eksisterende `CommandRunner`.
6. Lagre kjøringen, gruppene og medlemskapene i OpenCLIP-databasen.
7. Vis ferdige kjøringer og grupper i webgrensesnittet.
8. Gjenbruk den felles bildebrowseren for alle bildene i én gruppe.
9. La brukeren slette en kjøring gjennom en eksplisitt handling.

Launcheren skal være den eneste brukerflaten som kan starte den tunge jobben.
Webserveren skal bare vise og slette resultater, og skal ikke ha ruter eller en
jobbmanager for å starte eller avbryte gruppering. En fast, intern
workerkommando kan brukes som underprosessgrense for launcheren, på samme måte
som andre tunge launcherjobber. Den skal ikke dokumenteres som en selvstendig
CLI-arbeidsflyt.

## Eksisterende arkitektur

### `files` og `file_sources`

Hoveddatabasen har schema v22. `files` inneholder én kanonisk rad per lagret
fil. `file_sources` er importproveniens og kan ha flere rader som peker på den
samme `files.id` når samme filinnhold er funnet i flere importer.

Grupperingen skal derfor alltid arbeide med unike `files.id`. Et utvalg basert
på en import skal bruke samme `EXISTS`-semantikk mot `file_sources` som dagens
filtersøk. Et bilde med flere importreferanser skal aldri forekomme flere
ganger i algoritmens input.

Bare aktive rader med `files.deleted_at IS NULL` skal være kvalifisert.
`is:deleted` skal avvises som grupperingsutvalg i første versjon.

### OpenCLIP-databasen

OpenCLIP-data ligger i den separate sidecar-databasen:

```text
.bilder-openclip.sqlite3
```

Før denne leveransen var OpenCLIP-schemaet v1 og hadde tabellene:

- `meta`
- `image_embeddings`
- `image_search_runs`
- `image_search_results`

En embedding identifiseres av:

```text
(file_id, model_name, pretrained)
```

Embeddingvektoren er lagret som en `float32`-BLOB. Nye embeddings normaliseres
før lagring. Websøket normaliserer dessuten vektorene på nytt når søkematrisen
bygges. Databasen lagrer ikke dimensjonen og har ingen constraint for norm,
endelige verdier eller lik dimensjon.

Standardmodellen `ViT-B-32 / laion2b_s34b_b79k` har 512 dimensjoner. Den
støttede `ViT-L-14 / laion2b_s32b_b82k` har 768 dimensjoner. Bildebank tillater
også en lokal, egendefinert modellfil. Grupperingskoden må derfor utlede
dimensjonen fra dataene og aldri hardkode 512 eller 768.

### Bilder uten embedding

`image-scan` lager bare embeddings for aktive stillbilder. Videoer får ikke
OpenCLIP-embedding. Eksisterende søk bruker bare de aktive embeddingradene som
finnes og feiler hvis det ikke finnes noen.

En grupperingskjøring skal være mer eksplisitt og registrere:

- antall valgte filer
- antall valgte stillbilder
- antall stillbilder med gyldig embedding for valgt modell
- antall uten embedding
- antall med ugyldig embedding
- antall som faktisk ble gruppert

Noen manglende embeddings skal ikke gjøre hele kjøringen til en feil. Ingen
gyldige embeddings skal gi en tydelig feilet kjøring uten cluster-rader.

### Likhetssøk som kan gjenbrukes

`bildebank/server_search.py` har allerede kode for å:

- dekode embedding-BLOB-er med NumPy
- normalisere vektorer
- bygge en tett `float32`-matrise
- avgrense på `(model_name, pretrained)`
- koble embeddingene mot aktive `files`-rader

Den eksisterende loaderen gjør i dag en vektor med avvikende dimensjon om til
en nullrad uten å rapportere feilen. Gruppering trenger strengere validering.
Den lille, generelle vektordelen bør trekkes ut til en felles hjelpefunksjon
eller modul som både søk og gruppering kan bruke. Domenekode for gruppering bør
ikke importere servermodulen.

### Utvalg og felles browser

Dagens filtersøk kan uttrykke blant annet:

- `after:` og `before:` eller år/måned/dag
- `source:` for import/kilde
- `person:`
- `tag:`
- `location:`
- kombinasjoner av kriteriene

`BrowserSource`, `source_item_ids()` og filtersøkets eksisterende
databasevedlegg skal gjenbrukes. Tomt filter skal bety alle aktive bilder.

Når en gruppe vises, bør gruppen representeres som en ny `BrowserSource`.
Hoveddatabasen kan vedlegge OpenCLIP-databasen read-only og velge filer med en
subquery mot `image_cluster_members`. Da får gruppen den vanlige navigasjonen,
full bildevisning, dato, bildeinfo, rotering og lenkestruktur uten en parallell
browser.

### Launcher og tunge jobber

`devel-docs/web-background-jobs.txt` er en utsatt plan. Serveren har ingen
generell jobbmanager nå. Modell-forhåndslasting har en liten egen tråd, mens
andre tunge serveroperasjoner i hovedsak venter i HTTP-kallet.

Launcheren har allerede den riktige prosessmodellen. `launcher_tools_tab.py`
starter blant annet `face-scan` og `image-scan` som faste kommandoer,
`launcher_commands.py` bygger argumentlistene, og `CommandRunner` i
`launcher_runner.py` eier én avbrytbar underprosess om gangen. Output sendes
til launcherloggen, og knappen **Avbryt jobb** sender et kontrollert avbrudd.

Grupperingen skal følge dette mønsteret:

- Launcheren samler inn og validerer brukerens filter, antall grupper og seed.
- En ren kommando-bygger lager en fast argumentliste uten shellstreng.
- Selve utvalget, valideringen og grupperingen kjører i en separat
  Python-prosess, ikke i Tk-prosessen eller webserveren.
- Workerprosessen skriver stadium, tellinger og run-ID som strukturert,
  menneskelesbar progresjon til stdout.
- `CommandRunner` eier prosessen og kontrollert avbrudd.
- Webserveren leser ferdige og historiske runs, men starter aldri jobben.

Dette holder Tk-vinduet responsivt, unngår at tunge NumPy/scikit-learn-importer
blir liggende i launcherprosessen, og krever ingen ny generell jobbmanager i
serveren. Domenefunksjonen skal fortsatt være uavhengig av launcheren slik at
den kan testes direkte.

## Utvalgsmodell

Første versjon trenger to utvalgstyper:

```json
{"kind": "all", "hide_out_of_focus": false}
```

eller:

```json
{
  "kind": "filter",
  "query": "year>=1990 year<=1999 tag:Ferie",
  "hide_out_of_focus": false
}
```

Filteret skal parses og kanoniseres med dagens parser før det lagres. Resultatet
skal være unike, deterministisk sorterte `file_id`-er.

Launcher-dialogen bør ha et eksplisitt valg for «Skjul ute av fokus», med
standard `false`, og lagre den effektive verdien i `selection_json`. Utvalget
skal ikke avhenge av en skjult browserinnstilling. Webgrensesnittet oppretter
ikke runs og trenger derfor ikke et tilsvarende opprettingsvalg.

Første versjon trenger ikke lagre alle valgte file-ID-er som egne inputrader.
Cluster-medlemmene lagrer de bildene som faktisk ble gruppert, mens run-raden
lagrer det kanoniske filteret og tellingene. Et hash-fingerprint av sorterte
`(file_id, sha256, updated_at)` fra `image_embeddings` kan eventuelt lagres for
å gjøre inputen lettere å sammenligne.

## Validering av embeddingmatrisen

For hver valgt embedding skal koden kontrollere:

1. BLOB-lengden er positiv og delelig med fire.
2. Vektoren kan leses som `float32`.
3. Alle verdier er endelige.
4. Normen er større enn null.
5. Dimensjonen er lik dimensjonen for resten av kjøringen.
6. `image_embeddings.sha256` stemmer med den aktive `files.sha256`.
7. Modellnavn og pretrained er eksakt den valgte modellnøkkelen.

Gyldige vektorer normaliseres på nytt. Ugyldige rader hoppes over og telles.
Kjøringen skal ikke reparere, overskrive eller slette en ugyldig embedding.

Inputmatrisen sorteres etter `file_id`. Dette er nødvendig for så god
reproduksjon som mulig med fast seed.

## Første algoritme: MiniBatchKMeans

`MiniBatchKMeans` anbefales fordi den er enkel, kjent og praktisk for rundt
20 000 bilder. Scikit-learn må legges til den valgfrie OpenCLIP-profilen og den
komplette Windows CPython 3.13-låsen.

Alle effektive parametere må angis eksplisitt og lagres. Anbefalt første
konfigurasjon er:

```text
algorithm=minibatch_kmeans
n_clusters=<brukerens valg>
batch_size=1024
random_state=<brukerens seed>
n_init=<eksplisitt heltall>
max_iter=<eksplisitt heltall>
reassignment_ratio=<eksplisitt verdi>
```

Eksakte standardverdier skal besluttes og testes sammen med den låste
scikit-learn-versjonen. Ikke stol på bibliotekets standardverdier; de kan
endres mellom versjoner.

### Avstand og representanter

K-means kjøres på L2-normaliserte vektorer. Første versjon bruker euklidsk
avstand fra hvert bilde til det tildelte sentroidet.

Innen en gruppe sorteres medlemmene deterministisk etter:

```text
(distance_to_center, file_id)
```

Første medlem er representativt bilde. De neste medlemmene brukes som små
forhåndsvisninger. Denne rangeringen lagres som `center_rank`.

Hvis flere bilder har lik avstand, avgjør `file_id`. Dersom representanten
senere fjernes fra sidecar-dataene, blir neste aktive medlem representant uten
at gruppen må kjøres på nytt.

### Færre bilder enn grupper

Hvis antall gyldige embeddings er mindre enn ønsket `n_clusters`, skal
kjøringen feile før scikit-learn kalles. Antall grupper skal ikke reduseres
automatisk.

Hvis mange identiske eller nesten identiske embeddings gir færre ikke-tomme
grupper enn ønsket, skal kjøringen fullføres med `actual_cluster_count` og en
advarsel. Tomme grupper skal ikke vises som vanlige kort.

### Klyngenumre

Scikit-learns cluster-label er bare et internt resultat for én kjøring.
`algorithm_label` skal aldri brukes som varig identitet eller sammenlignes
mellom kjøringer.

Databasen bruker en egen `image_clusters.id`. Ved fullføring sorteres gruppene
etter opprinnelig medlemstall, største først. Likhet brytes deterministisk med
representantens `file_id`, og rekkefølgen lagres som `display_order`.
Rekkefølgen beregnes ikke på nytt når medlemmer senere forsvinner.

UI-et kan vise «Gruppe 1» basert på `display_order`, men teksten må gjøre det
klart at nummeret bare gjelder den aktuelle kjøringen. En ny kjøring kan gi
andre grupper og andre numre.

## Foreslått OpenCLIP-schema v2

Grupperingsdata er avledet fra OpenCLIP og skal ligge i OpenCLIP-databasen.
Hoveddatabasens schema skal ikke endres for første versjon.

Dette er en bevisst sikkerhetsgrense: `.bilder-openclip.sqlite3` kan
reproduseres eller fjernes uten å endre bildesamlingens autoritative data.
Hoveddatabasen leses for utvalg og oppsummering, men får ingen
grupperingstabeller, cluster-ID-er eller migrering for denne funksjonen.

### `image_clustering_runs`

Foreslåtte felt:

```sql
CREATE TABLE image_clustering_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_kind TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    model_name TEXT NOT NULL,
    pretrained TEXT NOT NULL,
    embedding_dimension INTEGER,
    algorithm TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    random_seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    selected_file_count INTEGER NOT NULL DEFAULT 0,
    selected_image_count INTEGER NOT NULL DEFAULT 0,
    embedded_file_count INTEGER NOT NULL DEFAULT 0,
    missing_embedding_count INTEGER NOT NULL DEFAULT 0,
    invalid_embedding_count INTEGER NOT NULL DEFAULT 0,
    clustered_file_count INTEGER NOT NULL DEFAULT 0,
    actual_cluster_count INTEGER NOT NULL DEFAULT 0,
    warning_message TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Schemaet skal validere tillatte verdier med `CHECK` der det er hensiktsmessig:

- `selection_kind`: `all`, `filter`
- `status`: `pending`, `running`, `completed`, `failed`, `cancelled`
- tellinger skal ikke være negative
- dimensjonen skal være positiv når den er satt

### `image_clusters`

```sql
CREATE TABLE image_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL
        REFERENCES image_clustering_runs(id) ON DELETE CASCADE,
    algorithm_label INTEGER NOT NULL,
    display_order INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'cluster',
    center_embedding BLOB,
    UNIQUE(run_id, algorithm_label),
    UNIQUE(run_id, display_order),
    UNIQUE(id, run_id)
);

CREATE INDEX idx_image_clusters_run_id
ON image_clusters(run_id, display_order);
```

`kind` kan være `cluster` i første versjon og gjør det mulig å representere
`noise` ved en senere HDBSCAN-implementasjon. Sentroidet er nullable av samme
grunn.

### `image_cluster_members`

```sql
CREATE TABLE image_cluster_members (
    run_id INTEGER NOT NULL,
    cluster_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    distance_to_center REAL,
    center_rank INTEGER,
    membership_score REAL,
    PRIMARY KEY(cluster_id, file_id),
    UNIQUE(cluster_id, center_rank),
    UNIQUE(run_id, file_id),
    FOREIGN KEY(cluster_id, run_id)
        REFERENCES image_clusters(id, run_id) ON DELETE CASCADE
);

CREATE INDEX idx_image_cluster_members_file_id
ON image_cluster_members(file_id);
```

`file_id` kan ikke ha en SQLite-foreign key til hoveddatabasen fordi tabellene
ligger i forskjellige databasefiler. Livssyklus og doctor må håndheve denne
koblingen eksplisitt.

`run_id` er bevisst duplisert på medlemsraden. Den sammensatte foreign key-en
sikrer at den matcher clusterets run, mens `UNIQUE(run_id, file_id)` hindrer at
samme bilde havner i flere grupper i samme kjøring.

### Hva som ikke lagres

Første versjon trenger ikke cache følgende på cluster-raden:

- medlemstall
- representativ `file_id`
- dato fra/til
- vanligste metadata

Dette kan beregnes fra `image_cluster_members` og gjeldende aktive
hoveddatabase. Dermed blir neste medlem automatisk representant hvis en fil
fjernes, og metadataendringer vises uten å kjøre algoritmen på nytt.

`actual_cluster_count` og de opprinnelige tellingene på run-raden er
sluttstatistikk fra kjøringen. UI-et kan i tillegg vise dagens aktive
medlemstall og om resultatet har krympet siden kjøringen.

## OpenCLIP-migrering v1 til v2

Migreringen skal:

1. Kjøres i `BEGIN IMMEDIATE`.
2. Opprette bare de tre nye tabellene og indeksene.
3. Sette `meta.schema_version=2` etter vellykket oppretting.
4. Validere struktur, foreign keys og databasehelse før commit.
5. Rulle alt tilbake ved feil.
6. Ikke lese, dekode, skrive eller bygge om embedding-BLOB-er.

Dagens adopsjon av uversjonerte OpenCLIP-databaser må deles per versjon. En
komplett gammel struktur skal adopteres som v1 og deretter migreres til v2.
Valideringen kan ikke kreve v2-tabeller før adopsjonen er ferdig.

Read-only-kode skal aldri migrere. En eldre eksplisitt schema-versjon skal gi
en tydelig melding om at en skrivbar OpenCLIP-operasjon må forberede databasen.

## Run-livssyklus og transaksjoner

En kjøring følger denne flyten:

1. Ta `TargetLock` med et eget kommandonavn.
2. Opprett run-raden med `running` og commit.
3. Finn det valgte settet av unike aktive `files.id`.
4. Les og valider embeddingene.
5. Lukk eller avslutt enhver skrivetransaksjon før algoritmen starter.
6. Kjør MiniBatchKMeans.
7. Beregn avstand og deterministisk rangering.
8. Skriv alle `image_clusters` og `image_cluster_members` i én kort
   transaksjon.
9. Sett run-raden til `completed` i samme transaksjon.

`TargetLock` kan holdes gjennom hele kjøringen i første versjon. Det er
konservativt og samsvarer med dagens `image-search`, men kan blokkere andre
samlingsoperasjoner mens CPU-jobben pågår. Et senere ytelsessteg kan slippe
låsen under selve algoritmen dersom alle inputidentiteter revalideres før
resultatet lagres.

Ved validerings- eller algoritmefeil:

- rollback alle cluster- og medlemsrader
- sett run-raden til `failed`
- lagre en kort, brukerrettet `error_message`
- behold run-raden til brukeren sletter den eksplisitt
- behold tekniske detaljer i logg/traceback, ikke i webgrensesnittet

Ved kontrollert `Ctrl-C`:

- sett run-raden til `cancelled`
- lagre sluttid
- ikke behold delvise cluster- eller medlemsrader
- behold run-raden til brukeren sletter den eksplisitt
- la workerprosessen returnere vanlig avbruddskode slik at launcheren viser
  jobben som kontrollert avbrutt

Et hardt prosessavbrudd kan etterlate `running`. Neste skrivbare
grupperingsoperasjon kan, etter at den har tatt `TargetLock`, markere eldre
`running`-rader som avbrutt. Et read-only GET-oppslag skal ikke gjøre denne
reparasjonen.

## Bildelivssyklus

Grupperingsdata skal følge samme regler som øvrige OpenCLIP-data:

- `remove` sletter medlemskap for filen i alle runs.
- `undelete` gjenoppretter ikke medlemskap eller embedding.
- `unimport` med andre gjenværende `file_sources` beholder medlemskapet.
- `unimport` som fjerner siste kilde og selve `files`-raden, sletter
  medlemskapet.
- purge trenger normalt ingen ekstra gruppering fordi medlemskapet allerede
  ble fjernet ved `remove`, men integritetskontrollen skal tåle rester.

`bildebank/item_sidecars.py` må slette `image_cluster_members` for berørte
`file_id`-er i den samme ATTACH-transaksjonen som resten av remove/unimport.
Tomme cluster-rader kan slettes etterpå. Run-raden skal beholdes som historikk
med null aktive grupper til brukeren eksplisitt sletter kjøringen.

`cleanup-image-search` og doctor må utvides eller suppleres slik at
foreldreløse cluster-medlemmer kan oppdages og ryddes kontrollert. Ingen slik
opprydding skal påvirke bilder eller embeddingene.

## Oppsummering per gruppe

Gruppekortet beregnes fra dagens aktive medlemmer.

### Antall og representanter

- medlemstall: antall aktive medlemmer som fremdeles finnes i hoveddatabasen
- representant: laveste tilgjengelige `center_rank`
- forhåndsvisninger: de neste fire eller fem tilgjengelige medlemmene
- avstand: vises i detaljvisning eller sortert medlemsliste, ikke nødvendigvis
  på hvert kort

En aktiv databasepost med manglende mediefil skal ikke slettes eller repareres
automatisk. Kortet skal tåle en mislykket thumbnail og vise plassholder eller
neste forhåndsvisning. Grupperingen kan bruke den lagrede embeddingen uten å
åpne originalfilen.

### Dato

Bruk samme effektive dato som bildebrowseren. Ukjente datoer utelates fra
minimum/maksimum og telles separat. UI-et viser for eksempel:

```text
1998-06-14 – 2004-12-24 · 3 uten kjent dato
```

Usikre manuelle datointervaller skal bruke de faktiske yttergrensene
`manual_date_from` og `manual_date_to` i gruppeoppsummeringen. Dagens midtdato
beholdes for sortering. Ukjente datoer telles og vises separat.

### Tagger

Finn de vanligste taggene med antall distinkte medlemsfiler. Vis maksimalt tre
og oppgi antall, for eksempel `Ferie (42)`. Systemtagger og brukertagger kan
vises, men «Ute av fokus» må ikke skjules dersom det faktisk er vanlig i
gruppen.

### Personer

Første versjon skal:

- tell bekreftede `person_faces`
- tell manuelle `person_files`
- dedupliser per `(person_id, file_id)`
- ikke tell `face_suggestions` som etablert metadata

`face_suggestions` skal ikke telles eller vises som etablert metadata.

### Steder

Stedsoppsummering er mer tvetydig fordi navngitte H3-celler og definerte steder
kan overlappe. Stedstekst utsettes i første versjon; gruppekortene viser dato,
tagger og bekreftede/manuelt tilordnede personer. Det skal ikke innføres en ny
definisjon av «sted» bare for grupperingen.

## Launcher og intern workerkommando

### Brukerflate i launcheren

**Verktøy**-fanen får en knapp, for eksempel **Grupper bilder …**, som åpner
en liten dialog med:

- `Algoritme`, MiniBatchKMeans som standard eller HDBSCAN
- for MiniBatchKMeans: `Antall grupper`, positivt heltall med standard 20
- for MiniBatchKMeans: `Random seed`, standard 0
- for HDBSCAN: `Minste gruppestørrelse`, minst 2 og standard 5
- for HDBSCAN: valgfri `Min samples`; tomt bruker minste gruppestørrelse
- `Filter`, valgfritt; tomt betyr alle aktive bilder
- `Skjul ute av fokus`, eksplisitt av/på og avslått som standard

`batch_size`, `n_init`, `max_iter` og `reassignment_ratio` skal bruke
eksplisitte, låste og dokumenterte standarder. De skal ikke være synlige i
første UI-versjon. Dialogen skal forklare at jobben bare lager et forslag, og
at ingen bilder eller eksisterende metadata endres.

Før oppstart skal launcheren kontrollere at:

- en samling er valgt
- filteret kan parses
- parameterne for valgt algoritme er gyldige heltall

OpenCLIP-oppsettet skal bruke den eksisterende automatiske installasjonsflyten.
En lett importkontroll skal verifisere at både `open_clip` og `sklearn` er
tilgjengelige. Hvis den komplette, låste OpenCLIP-pakken mangler eller er
ufullstendig, skal launcheren tilby å kjøre den eksisterende
`install-openclip.ps1`-flyten og deretter kontrollere på nytt. Scikit-learn
skal aldri ha en egen installasjonskommando eller en egen, ulåst versjon.

Selve jobben kontrollerer at det finnes embeddings for den konfigurerte
modellnøkkelen, og at antallet gyldige embeddings er tilstrekkelig for valgt
algoritme og parametere.
OpenCLIP-modellfilen trenger ikke lastes inn for gruppering av allerede lagrede
vektorer.

Under kjøringen skal launcherloggen vise:

- run-ID
- modell, pretrained og utledet dimensjon
- antall valgt, uten embedding, ugyldig og gruppert
- ønsket og faktisk antall grupper
- stadium for utvalg, innlasting, algoritme og lagring
- algoritme og alle effektive algoritmeparametere
- størrelsen på de største gruppene ved fullføring

MiniBatchKMeans og HDBSCAN gir ikke nyttig kontinuerlig prosentprogresjon. I
algoritmefasen viser workerprosessen derfor teksten «Grupperer bilder …» og
forsøker å skrive forløpt tid omtrent hvert femte sekund. Heartbeaten kjører i
en sovende daemontråd, flusher hver linje og stoppes før lagringssteget. Native
algoritmekode kan holde workerens Python-tråder bundet. Launcheren driver derfor
i tillegg sin egen sekundviser når startlinjen er mottatt, og erstatter samme
progresjonslinje hvert sekund til neste workerlinje eller prosessavslutning. Det
skal ikke oppgis en misvisende prosent eller beregnet gjenstående tid.

### Intern prosessgrense

Launcheren trenger en fast intern workerkommando fordi `CommandRunner` kjører
underprosesser. `launcher_commands.py` bør få en ren bygger, for eksempel
`image_clustering_command(...)`, som sender samlingssti og validerte parametere
som separate argumenter.

Workerkommandoen kan implementeres som en intern, ikke-dokumentert
CLI-subkommando og rutes videre til den samme domenetjenesten som testene
kaller direkte. Den er en teknisk prosessgrense for launcheren, ikke en
alternativ brukerarbeidsflyt. Det skal derfor ikke lages offentlige
`image-cluster`, `image-cluster-list` eller `image-cluster-delete`-kommandoer i
brukerdokumentasjonen.

Run-liste og sletting hører til webgrensesnittet. Slettingen skal fortsatt
verifisere og teste at den ikke endrer `files`, `file_sources`, tags, personer,
steder, `image_embeddings`, mediefiler eller avledede filer.

## Webgrensesnitt

### Ruter i første leveranse

```text
GET  /grouping
GET  /grouping/runs/<run_id>
GET  /grouping/runs/<run_id>/clusters/<cluster_id>
GET  /grouping/runs/<run_id>/clusters/<cluster_id>/item/<file_id>
POST /grouping/runs/<run_id>/delete
```

`GET /grouping` viser en liste over kjøringer. `GET`-rutene skal åpne hoved- og
OpenCLIP-databasen read-only og skal ikke opprette, adoptere eller migrere
schema. Listen skal også vise feilede og avbrutte kjøringer med status og kort,
brukerrettet feilmelding. De beholdes til brukeren sletter dem.

Sletting krever normal CSRF-beskyttet POST og en tydelig bekreftelse som viser
run-ID, filter og antall cluster-/medlemsrader. Bekreftet sletting fjerner
run-raden permanent; foreign keys kaskader til clusters og medlemmer. Det
innføres ikke myksletting for disse reproduserbare sidecar-dataene.

POST-ruten skal ta `TargetLock` med eget kommandonavn før den åpner
OpenCLIP-databasen for skriving. Dermed kan den ikke slette en run samtidig med
launcherens aktive grupperingsjobb eller en annen samlingsoperasjon.

### Run-side

Vis:

- filter eller «Alle aktive bilder»
- modell og dimensjon
- algoritme og parametere
- seed
- tidspunkt og status
- valgt, gruppert, manglende og ugyldige embeddings
- eventuell feil eller advarsel
- grupper som kort
- knapp for å slette kjøringen

### Gruppekort

Hvert kort viser:

- representativ thumbnail
- 3–5 små thumbnails nærmest senteret
- aktivt medlemstall
- datointervall og antall uten dato
- opptil tre vanlige tagger
- vanlige bekreftede/manuelt tilordnede personer
- lenke «Vis alle bildene»

Kortet skal ikke ha handlinger som endrer medlemsbildene i første versjon.

### Read-only og LAN-share

Eksisterende runs kan vises i vanlig lokal read-only-modus fordi GET-oppslagene
er rene lesinger. Jobboppstart finnes ikke i webgrensesnittet. Sletting er
utilgjengelig fordi all POST er blokkert i read-only.

Grupperingssidene skal blokkeres i LAN-share i første versjon.
`selection_json` kan inneholde filter på `sourcepath:` og dermed en lokal
maskinsti; denne informasjonen skal ikke sendes til LAN-klienter.

### Navigasjon, dashboard og innstillinger

- Vis «Gruppering» i topplinjen når OpenCLIP-funksjonen er aktivert.
- Dashboardet kan vise antall fullførte og feilede runs med et read-only
  statusoppslag.
- Innstillinger skal fortsatt eie bare OpenCLIP-aktivering og modellvalg.
- `n_clusters`, seed og andre run-parametere hører til launcher-dialogen, ikke
  globale innstillinger eller et webskjema.

## Avbrudd fra launcheren

Launcheren tillater allerede bare én aktiv `CommandRunner`-prosess om gangen.
Grupperingsjobben startes med `cancellable=True`, slik at den felles knappen
**Avbryt jobb** sender `SIGINT` på Linux eller det tilsvarende kontrollerte
signalet på Windows.

`MiniBatchKMeans.fit()` har ikke en naturlig cancel-callback. Første versjon
kan derfor behandle avbrudd når bibliotekskallet returnerer eller hever
`KeyboardInterrupt`. Run-raden skal da ende som `cancelled`, uten delvise
medlemsrader, og launcheren skal vise kontrollert avbrudd. Hvis rask avbryting
under selve algoritmefasen blir et krav, må algoritmen eventuelt drives med
kontrollerte `partial_fit`-batcher. Det påvirker resultat og reproduksjon og
skal være et eget designvalg, ikke en skjult endring.

Et hardt avsluttet launcher- eller workerprosess kan fortsatt etterlate en
`running`-rad. Recovery-regelen i run-livssyklusen gjelder derfor uavhengig av
launcherens in-memory-tilstand.

## HDBSCAN

Run-tabellen er algoritmeuavhengig. En algoritmeadapter bør returnere:

- label per inputrad
- eventuell sentroid eller medoid
- avstand eller annen representativ rangering
- medlems-score der algoritmen tilbyr det
- eksplisitte støy-/outlier-medlemmer

HDBSCAN-implementasjonen bruker:

- `algorithm=hdbscan`
- `min_cluster_size`
- `min_samples`
- valgt metrikk
- eventuelt `cluster_selection_method`

HDBSCAN velger antall grupper selv og kan merke bilder som støy. Støy lagres
som en `image_clusters`-rad med `kind='noise'`, nullable sentroid og nullable
`distance_to_center`, og vises separat som «Ugrupperte bilder».

Første implementasjon bruker scikit-learns HDBSCAN med euklidsk avstand på de
L2-normaliserte embeddingene, `cluster_selection_method='eom'`, én jobb og
lagret medoid. `probabilities_` lagres som `membership_score`. Normalgrupper
rangeres etter avstand til medoid; støy rangeres deterministisk etter
`file_id`.

For normaliserte embeddings må euklidsk kontra cosinusmetrisk vurderes og
benchmarkes. HDBSCAN på 20 000 vektorer med 512 eller 768 dimensjoner kan ha
vesentlig høyere ressursbruk enn MiniBatchKMeans og må testes på støttet
Windows-plattform før det tilbys.

Eventuell PCA eller annen dimensjonsreduksjon er en egen transformasjon. Hvis
den innføres, må transformasjonstype, dimensjon, seed og bibliotekversjon
lagres som del av run-parametrene.

## Testplan

### Schema og migrering

- Ny OpenCLIP-database opprettes direkte som v2.
- En v1-database migreres til v2 uten at eksisterende embedding-BLOB-er eller
  søkeresultater endres.
- En komplett uversjonert v1-database adopteres og migreres.
- Mangelfullt eller nyere schema avvises uten endringer.
- Sen migreringsfeil ruller tilbake alle nye tabeller og schema-versjonen.
- Read-only-åpning migrerer aldri.

### Utvalg

- Tomt filter velger alle aktive stillbilder.
- Filtersøk med dato, import, person, tagg og sted gir samme file-ID-er som
  bildebrowseren.
- Flere `file_sources` for samme fil gir bare ett inputbilde.
- `is:deleted` avvises.
- Videoer og andre filer telles som ikke kvalifiserte og grupperes ikke.
- Tomt utvalg gir en forståelig feilet run uten cluster-rader.

### Embeddings

- Noen bilder uten embedding hoppes over og telles.
- Alle bilder uten embedding gir feil uten å endre embeddings.
- Feil BLOB-lengde, avvikende dimensjon, nullvektor, NaN og Inf håndteres.
- Modellnøkkelen må stemme eksakt.
- Vektorene normaliseres på nytt før algoritmen.
- Tellingene skiller eksplisitt mellom gyldige, manglende og ugyldige
  embeddings.

### Algoritme

- Færre bilder enn ønsket antall grupper feiler før scikit-learn kalles.
- Fast seed, fast inputrekkefølge og låst bibliotekversjon gir samme
  medlems-partisjon.
- Workerens `random_state` er nøyaktig seed-verdien fra launcherjobben og
  run-raden.
- Reproduksjonstesten sammenligner medlemssett, ikke algoritmens labelnummer.
- Representanten er medlemmet nærmest senteret.
- Lik avstand brytes deterministisk med `file_id`.
- `center_rank` og forhåndsvisningsrekkefølge er stabile.
- Identiske vektorer/tomme K-means-grupper gir kontrollert advarsel og korrekt
  faktisk gruppetall.
- `display_order` sorterer opprinnelig medlemstall synkende og bryter likhet
  med representantens `file_id`.
- Lagret `display_order` endres ikke når medlemmer senere forsvinner.

### Status og transaksjoner

- Vellykket kjøring går fra `running` til `completed`.
- Validerings- og algoritmefeil gir `failed` og ingen delvise medlemsrader.
- Kontrollert avbrudd gir `cancelled` og ingen delvise medlemsrader.
- Feilede og avbrutte run-rader beholdes til eksplisitt sletting.
- Hardt etterlatt `running` håndteres bare under senere skrivbar recovery.
- En feil under sluttinnsetting ruller tilbake både clusters og medlemmer.
- Databaseinnsetting av samme `file_id` i to clusters i samme run avvises.
- Databaseinnsetting med medlemsradens `run_id` forskjellig fra clusterets
  `run_id` avvises.

### Launcher

- Kommandobyggeren sender algoritme, filter, relevante algoritmeparametere og
  `hide_out_of_focus` som separate, eksakte argumenter.
- Tomt filter blir eksplisitt tolket som alle aktive bilder.
- Dialogen starter med MiniBatchKMeans, 20 grupper, seed 0 og «Skjul ute av
  fokus» avslått. HDBSCAN starter med minste gruppestørrelse 5 og tom
  `min_samples`.
- Avanserte MiniBatchKMeans-parametere vises ikke i dialogen.
- Ugyldig filter og ugyldige tall avvises før workerprosessen startes.
- Grupperingsknappen er utilgjengelig uten valgt samling eller nødvendig
  installert algoritmestøtte.
- Manglende `sklearn` utløser den samme låste OpenCLIP-installasjonsflyten
  som manglende `open_clip`; ingen separat installasjonskommando brukes.
- Jobben startes gjennom `CommandRunner` med `cancellable=True`.
- Progresjonslinjer erstatter tidligere linje i launcherloggen på samme måte
  som andre tunge jobber.
- **Avbryt jobb** gir `cancelled` run og kontrollert launcherstatus.
- Suksessmeldingen viser run-ID og forteller at resultatet finnes under
  **Gruppering** i webgrensesnittet. Launcheren starter eller åpner ikke
  serveren automatisk.
- Det finnes ingen web-POST eller offentlig CLI-arbeidsflyt som starter en run.

### Fil- og sidecarlivssyklus

- `remove` sletter cluster-medlemskap, men ingen ekstra bildefil.
- `undelete` gjenoppretter ikke medlemskap.
- Unimport med en gjenværende `file_sources`-rad beholder medlemskap.
- Unimport av siste kilde fjerner medlemskap sammen med resten av sidecar-dataene.
- En aktiv databaseført fil som mangler på disk endres ikke automatisk og
  krasjer ikke gruppesiden.
- Doctor og cleanup rapporterer foreldreløse medlemmer.

### Sletting av run

- Avbrutt bekreftelse endrer ingenting.
- Bekreftet POST sletter bare valgt run, clusters og medlemmer.
- Bekreftelsen viser run-ID, filter og antall cluster-/medlemsrader.
- POST-sletting tar `TargetLock` og avvises med en tydelig melding mens en
  grupperingsjobb eller annen samlingsoperasjon holder låsen.
- Andre runs beholdes.
- `files`, `file_sources`, tags, personer, steder, embeddings og søkeresultater
  er byte-/radmessig uendret.
- Ingen mediefil, thumbnail eller videopreview berøres.

### Web

- Run- og gruppesider fungerer med read-only databaseåpninger.
- Ingen web-rute kan starte eller avbryte den tunge jobben.
- Sletting krever skrivbar server, POST og CSRF.
- Gruppebrowseren bruker den felles browserflyten og utelater inaktive medlemmer.
- Cluster-identiteten mellomlagres mot OpenCLIP-databasens endringstid og størrelse,
  slik at bilde-for-bilde-navigasjon ikke validerer hele databasen på nytt.
- Et fjernet representativt bilde erstattes av neste aktive `center_rank`.
- Manglende thumbnail gir en kontrollert plassholder.
- Alle grupperingssider blokkeres i LAN-share.
- Klyngenummer presenteres bare innen den aktuelle kjøringen.

## Trinnvis implementeringsplan

### Trinn 1: Dokumenter produktkontrakten

- [x] Registrer avklarte produktbeslutninger i dette dokumentet.
- [x] Avgrens redigerbare algoritmevalg til antall grupper og seed.
- [x] Avgrens jobbstart til launcheren og resultatvisning/sletting til web.
- [x] Plasser alle nye grupperingstabeller i OpenCLIP-databasen.

Leveranse: ingen åpne produktvalg, ingen runtimeendring.

### Trinn 2: Avhengighet

- [x] Legg scikit-learn til `openclip`-ekstraen i `pyproject.toml`.
- [x] Regenerer komplett Windows CPython 3.13 OpenCLIP-lås.
- [x] Utvid `install-openclip.ps1` sin smoke-test til å importere og vise den
  låste scikit-learn-versjonen sammen med OpenCLIP og Torch.
- [x] Utvid dependency-lock-testene.
- [x] Fastsett eksplisitte verdier for `n_init`, `max_iter` og
  `reassignment_ratio` mot den låste scikit-learn-versjonen.
- [ ] Bekreft `batch_size=1024` med et representativt lite utvalg og omtrent
  20 000 embeddings.
- [x] Dokumenter alle effektive verdier for den senere felles parametermodellen.

Leveranse: installert og låst algoritmebibliotek med eksplisitte, testbare
standardverdier, uten funksjonell kobling.

### Trinn 3: OpenCLIP schema v2

- [x] Del schema-validering per OpenCLIP-versjon.
- [x] Implementer nye tabeller og indekser.
- [x] Legg inn medlemsradens `run_id`, sammensatt foreign key og
  `UNIQUE(run_id, file_id)`.
- [x] Implementer eksplisitt v1 til v2-migrering.
- [x] Oppdater read-only schema-gate.
- [x] Skriv migrerings-, rollback- og databevaringstester.
- [x] Oppdater `devel-docs/openclip-database.md`.

Leveranse: tomme grupperingstabeller, eksisterende OpenCLIP-funksjon uendret.

### Trinn 4: Felles embedding-loader

- [x] Implementer streng BLOB-/dimensjons-/finite-/normvalidering.
- [x] Returner matrise, file-ID-er, modell og tellinger.
- [x] Gjenbruk loaderen i serverens likhetssøk der det er lokalt og trygt.
- [x] Skriv isolerte tester for alle ugyldige vektorvarianter.

Leveranse: robust modellspesifikk embeddingmatrise uten gruppering.

### Trinn 5: Ren algoritmekjerne

- [x] Lag dataklasser for parametere og resultat.
- [x] Implementer MiniBatchKMeans-adapter.
- [x] Beregn avstand, representant og `center_rank`.
- [x] Gjør sortering og tie-break deterministisk.
- [x] Skriv algoritmetester uten SQLite.

Leveranse: ren funksjon fra matrise til grupperingsresultat.

### Trinn 6: Utvalg og run-tjeneste

- [x] Gjenbruk eksisterende filterparser og browserutvalg.
- [x] Opprett og oppdater run-status.
- [x] Koble utvalg, embedding-loader og algoritmekjerne.
- [x] Skriv resultatet atomisk.
- [x] Håndter tomt utvalg, manglende embeddings, feil og Ctrl-C.
- [x] Skriv tjeneste- og transaksjonstester.

Leveranse: intern Python-API som kan kjøre og slette en run.

### Trinn 7: Launcherjobb

- [x] Legg til ren `image_clustering_command(...)`-bygger i
  `launcher_commands.py`.
- [x] Legg til en intern, ikke-dokumentert workerinngang som kaller
  run-tjenesten og returnerer vanlige prosesskoder.
- [x] Legg til **Grupper bilder …** og parameterdialog i
  `launcher_tools_tab.py`.
- [x] Utvid den eksisterende OpenCLIP-statusen med kontroll av hele
  grupperingsavhengigheten, og gjenbruk den vanlige automatiske
  OpenCLIP-installasjonsflyten dersom kontrollen feiler.
- [x] Koble jobben til `CommandRunner` med `cancellable=True`.
- [x] Legg til `Image-clustering` som progresjonsnøkkel i
  `launcher_runner.py`.
- [x] Skriv progresjon og lesbar oppsummering, inkludert run-ID, til
  launcherloggen.
- [x] Test kommandobygging, preflight, oppstart, progresjon, fullføring og
  avbrudd.

Leveranse: den tunge jobben kan bare startes fra launcheren og kjører uten å
blokkere Tk-prosessen.

### Trinn 8: Livssyklus og integritet

- [x] Oppdater ATTACH-opprydding ved remove/unimport.
- [x] Oppdater opprydding av foreldreløse OpenCLIP-rader.
- [x] Oppdater doctor-kontroller.
- [x] Test remove, undelete, unimport med/uten annen kilde og purge-forutsetninger.

Leveranse: cluster-medlemskap følger aktiv bildelivssyklus uten å påvirke filer.

### Trinn 9: Read-only queries og felles browser

- [x] Implementer run-, cluster- og medlemsoppslag read-only.
- [x] Utvid `BrowserSource` med cluster-utvalg.
- [x] Vedlegg OpenCLIP-databasen eksplisitt med `mode=ro`.
- [x] Gjenbruk kilde-, måned-, item- og navigasjonsflyten.
- [x] Skriv kontrakttester for gruppebrowseren.

Leveranse: alle bilder i en gruppe kan blas i den ordinære browseren.

### Trinn 10: Webvisning og sletting

- [x] Legg til `/grouping` og run-side.
- [x] Render gruppekort med thumbnails, dato og metadata.
- [x] Legg til navigasjonslenke og dashboardstatus.
- [x] Legg til CSRF-beskyttet, bekreftet sletting.
- [x] Håndter read-only og blokker grupperingssider i LAN-share.
- [x] Skriv server-, HTML- og sikkerhetstester.

Leveranse: ferdige launcher-runs kan brukes og slettes i webgrensesnittet.

### Trinn 11: Dokumentasjon og verifikasjon

- [x] Oppdater `app-design.md` med ikke-muterende grupperingsatferd og korrekt
  hovedschema-versjon.
- [x] Oppdater launcher-, OpenCLIP- og filtersøkdokumentasjon.
- [x] Oppdater `docs/start.md` og skriv `docs/web/gruppering.md`; ikke opprett
  en offentlig CLI-side for workerkommandoen.
- [x] Kjør fokuserte tester uten xdist.
- [x] Kjør full suite med `python -m pytest -n auto`.
- [ ] Test en disponibel Windows-samling med et lite filter og omtrent 20 000
  embeddings før funksjonen brukes på hovedsamlingen.

Leveranse: dokumentert og verifisert første versjon.

### Senere trinn: benchmark og eventuell raskere avbryting

- [ ] Benchmark MiniBatchKMeans på støttet Windows-installasjon.
- [ ] Vurder `partial_fit` bare dersom rask avbryting under algoritmen blir et
  eksplisitt krav.
- [x] Implementer algoritmeadapter for HDBSCAN.
- [x] Vis støybilder separat.
- [ ] Benchmark tid/minne og vurder eventuell dimensjonsreduksjon.

## Sannsynlige filer

### Nye filer

- `bildebank/image_clustering.py`
- `bildebank/server_endpoints_clustering.py`
- `tests/test_image_clustering.py`
- `tests/test_server_endpoints_clustering.py`
- `docs/web/gruppering.md`

### Eksisterende filer som sannsynligvis endres

- `bildebank/openclip.py`
- `bildebank/cli_image.py`
- `bildebank/cli.py`
- `bildebank/launcher_commands.py`
- `bildebank/launcher_status.py`
- `bildebank/launcher_setup_tab.py`
- `bildebank/launcher_tools_tab.py`
- `bildebank/launcher_runner.py`
- `bildebank/server_handler.py`
- `bildebank/server_browser_sources.py`
- `bildebank/server_browser_queries.py`
- `bildebank/server_shell.py`
- `bildebank/server_dashboard.py`
- `bildebank/server_app.py`
- `bildebank/item_sidecars.py`
- `bildebank/cli_doctor.py`
- `bildebank/assets/server.css`
- `pyproject.toml`
- `requirements/windows-py313-openclip.lock`
- `install-openclip.ps1`
- `tests/test_image_search_cli.py`
- `tests/test_launcher_commands.py`
- `tests/test_launcher_status.py`
- `tests/test_launcher_setup_tab.py`
- `tests/test_launcher_tools_tab.py`
- `tests/test_launcher_runner.py`
- `tests/test_item_sidecar_lifecycle.py`
- `tests/test_remove_undelete_cli.py`
- `tests/test_unimport_cli.py`
- `tests/test_server_browser_cli.py`
- `tests/test_dependency_locks.py`
- `docs/openclip.md`
- `docs/reference.md`
- `docs/start.md`
- `devel-docs/launcher.md`
- `devel-docs/openclip-database.md`
- `app-design.md`

Snapshotkoden forventes ikke å trenge funksjonell endring fordi hele
OpenCLIP-databasen allerede tas med. Tester som forventer OpenCLIP schema v1 må
likevel oppdateres til v2.

## Avklarte produktbeslutninger

Følgende er besluttet for første versjon:

1. Launcheren bruker ett felt med samme filtersøk som webgrensesnittet. Tomt
   felt betyr alle aktive bilder.
2. Antall grupper har standard 20 og kan endres av brukeren.
3. Bilder merket «Ute av fokus» er med som standard. Et eksplisitt, avslått
   valg kan skjule dem, og den effektive verdien lagres i run-utvalget.
4. Gruppekort viser dato, vanligste tagger og bekreftede/manuelt tilordnede
   personer. Steder utsettes.
5. Færre ikke-tomme grupper enn ønsket fullfører run med advarsel og faktisk
   gruppetall.
6. En run uten gjenværende aktive medlemmer beholdes som historikk til
   eksplisitt sletting.
7. Alle grupperingssider blokkeres i LAN-share i første versjon.
8. Ved fullføring viser launcheren run-ID og en suksessmelding, men starter
   eller åpner ikke webserveren automatisk.
9. Gruppeoppsummeringen bruker yttergrensene for usikre manuelle datoer og
   teller ukjente datoer separat.
10. Launcheren viser antall grupper og seed for MiniBatchKMeans, eller minste
    gruppestørrelse og valgfri `min_samples` for HDBSCAN. Andre parametere
    låses og dokumenteres internt.
11. Feilede og avbrutte runs beholdes og vises med status og kort feilmelding
    til brukeren sletter dem.
12. Bekreftet sletting fjerner run-, cluster- og medlemsradene permanent fra
    OpenCLIP-databasen. Det innføres ikke myksletting.
13. Grupper ordnes etter opprinnelig medlemstall, største først, med
    deterministisk tie-break. Lagret `display_order` endres ikke senere.

Det gjenstår ingen produktbeslutninger som blokkerer implementering. De
eksakte interne verdiene for `n_init`, `max_iter` og `reassignment_ratio` er en
teknisk oppgave i trinn 2 og skal fastsettes mot låst bibliotekversjon og
representative tester.

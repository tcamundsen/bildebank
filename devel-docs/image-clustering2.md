# Leiden-gruppering av bilder

Status: implementert med automatiserte tester. Nativ Windows-låsing og
installasjonstest samt manuell kalibrering på den faktiske samlingen gjenstår.

Dette dokumentet beskriver en tredje algoritme for automatisk gruppering av
bilder i Bildebank:

**k-nærmeste-nabo-graf kombinert med Leiden-gruppering.**

Bildebank har allerede MiniBatchKMeans og HDBSCAN. Leiden skal bygges inn i den
samme arkitekturen og først og fremst brukes til å undersøke om grafbasert
gruppering gir mer nyttige forslag på den faktiske bildesamlingen.

Relaterte dokumenter:

- `app-design.md`
- `devel-docs/image-clustering.md`
- `devel-docs/openclip-database.md`
- `devel-docs/launcher.md`
- `devel-docs/dependency-locks.md`

## Mål og avgrensning

Første leveranse skal:

1. Bygge en k-nærmeste-nabo-graf fra eksisterende OpenCLIP-embeddinger.
2. Bruke Leiden til å finne grupper i grafen.
3. Starte, følge og avbryte jobben gjennom launcherens eksisterende
   grupperingsflyt.
4. Lagre resultatet som avledede data i OpenCLIP-databasen.
5. Vise resultatet gjennom dagens grupperingssider og felles bildebrowser.
6. Lagre alle effektive parametere og nok statistikk til at kjøringer kan
   forstås og sammenlignes manuelt.
7. Aldri endre bilder, `files`, `file_sources`, tagger, personer, steder,
   kommentarer eller permanente samlinger.

Funksjonen er et utforskingsverktøy. En gruppe er bare et resultat i én
bestemt kjøring og blir ikke en varig egenskap ved et bilde.

Første leveranse skal teste selve metoden. Den skal ikke samtidig innføre en ny
jobbmanager, offentlig grupperings-CLI, automatisk parameterserie, avansert
sammenligningsanalyse eller permanent lagring av kNN-grafer.

## Bindende arkitekturvalg

### Launcher eier alle tunge jobber

Alle tunge jobber startes fra **Verktøy**-fanen i launcheren. Leiden skal bruke
samme mønster som MiniBatchKMeans og HDBSCAN:

- launcher-dialogen samler inn og validerer brukerens innstillinger
- `launcher_commands.py` bygger en fast argumentliste
- den interne `_image-clustering-worker`-kommandoen er prosessgrensen
- `CommandRunner` eier den avbrytbare underprosessen
- workerprosessen skriver stadier og tellinger til launcherloggen

Workerkommandoen skal ikke dokumenteres som en offentlig CLI-arbeidsflyt. Det
skal ikke opprettes en offentlig `bildebank cluster`-kommando.

Webserveren skal fortsatt bare:

- liste og vise kjøringer
- vise grupper gjennom den eksisterende bildebrowseren
- slette et resultat etter eksplisitt bekreftelse

Webserveren skal ikke starte, kopiere, gjenta eller avbryte grupperingsjobber.
Eksisterende regler for read-only og LAN-share beholdes.

### Eksisterende datakilder og identitet

Hoveddatabasens `files` inneholder én kanonisk rad per lagret fil.
`file_sources` er importproveniens og kan ha flere rader for samme `files.id`.
Et kildebasert filter skal derfor bruke dagens `EXISTS`-semantikk og aldri gi
samme bilde flere ganger.

Bare aktive stillbilder med `files.deleted_at IS NULL` er kvalifiserte.
Videoer er del av det valgte browserutvalget, men har ikke OpenCLIP-embedding og
grupperes ikke. De eksisterende tellingene for valgt, kvalifisert, manglende og
ugyldig input beholdes.

OpenCLIP-embeddingene ligger i `.bilder-openclip.sqlite3` og identifiseres av:

```text
(file_id, model_name, pretrained)
```

Den eksisterende validerte embeddingloaderen skal gjenbrukes. Den kontrollerer
blant annet SHA-256, dimensjon, endelige verdier og positiv norm, og returnerer
en deterministisk `float32`-matrise sortert etter `file_id`. Vektorene
normaliseres på nytt før gruppering.

### Hva «samme input» betyr

`selection_json` beskriver regelen som ble brukt for å velge bilder, for
eksempel alle aktive bilder eller et kanonisk filtersøk. Filteret evalueres på
nytt for hver kjøring. En senere kjøring med samme filter kan derfor få et annet
faktisk bildeutvalg dersom samlingen eller embeddingene er endret.

For hver kjøring skal det i tillegg lagres et `input_fingerprint`. Det beregnes
etter validering fra:

- modellnavn og `pretrained`
- embeddingdimensjon
- sorterte `file_id`-er
- de faktiske normaliserte `float32`-embeddingene som algoritmen mottar

Fingerprinten er SHA-256 over en entydig binær representasjon med eksplisitt
formatversjon. Den brukes bare til å fastslå om to kjøringer faktisk hadde
identisk algoritmeinput. Den er ikke en erstatning for `selection_json` og skal
ikke brukes som filidentitet.

En gammel kjøring er et historisk resultat. Bildebank lover ikke å kunne
rekonstruere nøyaktig samme input etter at bilder er fjernet eller embeddings
er bygget på nytt. Fast seed, identisk inputfingerprint, identiske parametere
og låste bibliotekversjoner er grunnlaget for en reproduksjonstest.

## Første minimumsleveranse

Følgende hører til første leveranse:

- Leiden som tredje valg i eksisterende launcher-dialog
- eksakt kNN-søk på den validerte embeddingmatrisen
- `union` og `mutual` som nabomodus
- uvektet graf og rå, ikke-negativ cosinuslikhet som kantvekt
- valgfri minste likhet
- CPM som Leiden-objektiv
- numerisk oppløsning og fast tilfeldig seed
- eksplisitte, lagrede interne Leiden-parametere
- isolerte bilder som «Ugrupperte bilder»
- dagens sentroidbaserte representant og forhåndsvisningsrekkefølge
- run-statistikk, avbrudd, feilbehandling og regresjonstester
- OpenCLIP-schemaendring og migrering uten omskriving av embeddings

Følgende utsettes til erfaringene med første leveranse er vurdert:

- profiler som «Grov», «Balansert» og «Detaljert»
- kopiering eller gjentakelse av en gammel kjøring fra brukergrensesnittet
- automatiske parameterserier
- side-ved-side-sammenligning
- grafbaserte kvalitetsmål, broer og avansert sentralitet
- modularitet som alternativ objektivfunksjon
- permanent eller delt grafcache
- tilnærmet nærmeste-nabo-søk
- den eksperimentelle modusen der gjensidige kanter får ekstra vekt

Disse utsettelsene skal ikke gjøre schemaet eller domenemodellen avhengig av at
det alltid finnes bare én algoritmevariant eller vektingsmetode.

## Grafbygging

### Input og små utvalg

La `n` være antall gyldige embeddings i kjøringen.

- `n = 0`: kjøringen feiler kontrollert som i dagens grupperingsflyt.
- `n = 1`: kjøringen feiler med en tydelig melding om at Leiden trenger minst
  to bilder.
- `n >= 2`: effektiv `k` er `min(requested_k, n - 1)`.

`requested_k` og `effective_k` skal begge lagres. Dersom `k` reduseres fordi
utvalget er lite, fullføres kjøringen med en advarsel. Launcher-dialogen
validerer `requested_k` som et heltall i intervallet 1–200. Første anbefalte
standard er 20; 10, 20 og 40 er nyttige manuelle startpunkter.

Nabosøket må be om søkebildet i tillegg til `k` naboer og fjerne nøyaktig
samme matriserad fra resultatet. Det er ikke tilstrekkelig å anta at første
treff alltid er søkebildet, fordi dupliserte embeddings kan gi like avstander.

### Nærmeste-nabo-søk

Første leveranse bruker eksakt søk på L2-normaliserte embeddings og
cosinuslikhet. Implementeringsplanen skal vurdere den konkrete, chunkede
utførelsen med scikit-learn `NearestNeighbors` eller tilsvarende eksisterende
NumPy-funksjonalitet. Bildebank har ingen eksisterende persistent
vektorindeks som kan gjenbrukes direkte som kNN-graf.

FAISS, HNSW og andre tilnærmede indekser skal ikke innføres før en måling på
omtrent 20 000 bilder viser at eksakt søk er upraktisk. En slik endring vil
kreve egen vurdering av avhengigheter, reproduksjon og indekslivssyklus.

### Nabomodus

Det rettede naboresultatet gjøres om til en enkel, urettet graf uten
selvkanter eller duplikate kanter.

- `union`: kanten A–B beholdes dersom A valgte B eller B valgte A.
- `mutual`: kanten A–B beholdes bare dersom A og B valgte hverandre.

Første standard er `union`, fordi den er mindre utsatt for mange isolerte
bilder. Nabomodus er en fast, lagret intern innstilling i launcheren; de
stabile verdiene er `union` og `mutual`.

Grafen bygges i denne faste rekkefølgen:

1. Finn de `effective_k` rettede naboene for hver node.
2. Fjern søkebildet selv.
3. Opprett urettede kandidater etter valgt nabomodus.
4. Gi hver kandidat en entydig cosinuslikhet.
5. Fjern kandidater under likhetsterskelen og kandidater med vekt 0.
6. Tell noder uten gjenværende kanter som isolerte.

«Kanter fjernet av terskelen» betyr dermed antall urettede kandidater fra trinn
3 som ikke overlever trinn 5, ikke antall rettede nabotreff.

### Minste likhet og kantvekter

Cosinuslikhet er en skår, ikke en sannsynlighet. Minste likhet og kantvekter
er faste, lagrede interne innstillinger i launcheren: terskelen er `0.0` og
vektene er `cosine`. Kanter med likhet lavere enn terskelen, eller vekt `0.0`,
fjernes slik at Leiden bare mottar positive vekter i vektet modus.

Negativ cosinuslikhet oppretter derfor aldri en kant i første leveranse.

Første leveranse støtter disse stabile verdiene for `weight_mode`:

- `unweighted`: alle beholdte kanter får vekt 1
- `cosine`: kantens vekt er den rå positive cosinuslikheten

Standard er `cosine`. Ved sammenslåing av rettede nabotreff brukes den høyeste
observerte likheten for den urettede kanten. Med eksakt cosinusberegning skal
verdiene normalt være like; regelen gjør resultatet entydig også ved små
numeriske avvik.

Følgende grafstatistikk beregnes og lagres:

- antall noder
- antall urettede kanter
- antall isolerte noder
- median likhet til nærmeste nabo før terskel
- median likhet til effektiv nabo nummer `k` før terskel
- antall kanter fjernet av terskelen

Statistikken beskriver den aktuelle kjøringen. Første leveranse trenger ikke en
egen forhåndsanalyse i launcher-dialogen.

## Leiden-partisjonering

### Objektiv og oppløsning

Første leveranse bruker CPM. Modularitet utsettes slik at den første testen har
færre bevegelige deler og en tydelig oppløsningsparameter.

Launcheren viser ett numerisk felt for oppløsning. Verdien skal være endelig og
større enn null. Det skal ikke innføres profiler før egnede verdier er målt på
en representativ del av bildesamlingen med den valgte grafmodusen og
vektsettingen.

Den foreløpige standardverdien for CPM-oppløsning er `0.2`. Verdien skal
kalibreres manuelt på den faktiske samlingen før funksjonen regnes som ferdig
verifisert. Bibliotekets standard skal ikke brukes skjult.

### Iterasjoner, seed og bibliotekversjon

Alle effektive Leiden-parametere skal angis eksplisitt i kode og lagres i
`parameters_json`. Dette omfatter minst:

- objektivfunksjon
- oppløsning
- antall iterasjoner eller eksplisitt «til stabilt resultat»
- tilfeldig seed
- vektmodus
- beta

Launcher-dialogen eksponerer ikke seed, iterasjonsvalg eller beta i første
brukergrensesnitt. De dokumenterte interne standardene er seed `0`,
`n_iterations=-1` for kjøring til stabilt resultat og `beta=0.01`. Alle tre
verdiene lagres i kjøringens parametere.

Navn og versjon for graf- og Leiden-biblioteket lagres i en validert
`library_versions_json`. Reproduksjon er et best mulig mål, ikke et løfte på
tvers av andre bibliotekversjoner eller plattformer.

### Isolerte og små grupper

Noder uten kanter sendes ikke inn i Leiden-partisjoneringen. De lagres samlet i
én `image_clusters`-rad med `kind='noise'` og vises med dagens tekst
**Ugrupperte bilder**. Rekkefølgen deres er deterministisk etter `file_id`.

Dersom alle noder er isolerte, skal kjøringen likevel fullføres. Resultatet får
én noise-gruppe, ingen ordinære grupper, `actual_cluster_count = 0` og en
brukerrettet advarsel om at alle bildene ble ugrupperte. Som for HDBSCAN teller
`actual_cluster_count` bare ordinære grupper og aldri noise-gruppen.

Alle ikke-isolerte noder beholder medlemskapet Leiden gir dem:

- en singleton-gruppe fra Leiden beholdes som en vanlig gruppe
- grupper med to eller tre bilder beholdes som vanlige grupper
- frakoblede komponenter behandles naturlig som separate deler av grafen
- ingen grupper slås sammen eller skjules på grunn av størrelse

Første leveranse har derfor ingen innstilling for minste synlige
gruppestørrelse. UI-gruppering under «Små grupper» kan vurderes senere, men må
da være ren presentasjon og ikke endre det lagrede algoritmeresultatet.

### Representativt bilde og medlemsrangering

Leiden-grupper skal gjenbruke dagens sentroidbaserte modell:

1. Beregn gjennomsnittet av de normaliserte embeddingene i gruppen.
2. Normaliser gjennomsnittsvektoren når normen er positiv.
3. Ranger medlemmene etter cosinusavstand til gjennomsnittet.
4. Bryt lik avstand med `file_id`.

Gjennomsnittsvektoren lagres i `image_clusters.center_embedding`, avstanden i
`image_cluster_members.distance_to_center` og rekkefølgen i `center_rank`.
Første aktive medlem etter denne rangeringen er representativt bilde. Dette
gjenbruker eksisterende schema, gruppevisning og fallback når et medlem senere
fjernes.

`membership_score` er `NULL` for Leiden i første leveranse. Vektet grad,
PageRank, svakeste medlem og broer mellom grupper utsettes til det finnes et
konkret behov.

## Datamodell og migrering

Grupperingsdata skal fortsatt ligge i OpenCLIP-databasen. Hoveddatabasens
schema skal ikke endres.

Eksisterende tabeller gjenbrukes:

- `image_clustering_runs`
- `image_clusters`
- `image_cluster_members`

`algorithm` får den stabile verdien `leiden`. Alle brukerparametere og
effektive interne parametere lagres i validert `parameters_json`. Sentrale
resultattellinger som brukes i oversikten skal ikke graves ut av fri JSON for
hvert sideoppslag.

Implementeringsplanen skal beskrive en additiv OpenCLIP-migrering fra v2 til
v3. Den skal minst gi run-raden plass til:

- `input_fingerprint` og fingerprint-formatversjon
- antall grafnoder, kanter og isolerte bilder
- antall kanter fjernet av terskelen
- nabosimilaritetsstatistikk
- `library_versions_json`

Alle nye v3-felt skal være nullable av hensyn til eksisterende
MiniBatchKMeans- og HDBSCAN-runs og Leiden-runs som feiler før grafen er
bygget. Numeriske felt skal ha `CHECK` som avviser negative verdier når feltet
ikke er `NULL`. En fullført Leiden-kjøring skal gjennom domenekoden alltid få
fingerprint, fingerprint-formatversjon, grafstatistikk og bibliotekversjoner.
Webvisningen skal tolke `NULL` som «ikke relevant eller ikke beregnet», ikke som
null noder eller null kanter.

Eksisterende tidsfelter brukes til total kjøretid. Det er ikke nødvendig med
en egen `duration`-kolonne dersom varigheten kan utledes entydig fra
`started_at` og `finished_at`.

Migreringen skal:

- kjøre i én transaksjon
- bevare alle embeddings, søk og tidligere grupperingskjøringer
- ikke dekode eller omskrive embedding-BLOB-er
- oppdatere schema-versjonen først etter vellykket strukturvalidering
- rulle fullstendig tilbake ved feil
- aldri startes av read-only weboppslag

Første leveranse trenger ikke `parent_run_id`, serie-ID, egne inputrader eller
tabeller for grafkanter. Slike felt innføres først når funksjonaliteten som
bruker dem er besluttet.

Bildelivssyklusen endres ikke. `remove` og unimport av siste kilde fjerner
medlemskap gjennom dagens sidecar-opprydding. Opprinnelige tellinger og
fingerprint på run-raden er historikk og beholdes. Sletting av en hel kjøring
fjerner bare run, grupper og medlemskap med eksisterende foreign-key-kaskade.

## Avhengigheter

Valgt implementasjon er `igraph==1.0.0` og
`Graph.community_leiden()` direkte. Den dekker CPM, vekter, oppløsning og
eksplisitte iterasjoner. Seed settes gjennom igraphs RNG-adapter. En egen
`leidenalg`-pakke er derfor ikke nødvendig.

Valget er kontrollert mot:

- offisiell støtte for CPM, vekter, oppløsning, seed og eksplisitte iterasjoner
- binærhjul for 64-bit CPython 3.13 på Windows
- installasjon og tester i WSL Debian
- lisens og API-stabilitet
- størrelse og transitive avhengigheter
- at funksjonaliteten i igraph er tilstrekkelig uten `leidenalg`

Løsningen legges i den komplette, valgfrie OpenCLIP-låsen. Det skal
ikke finnes en separat, ulåst installasjonsvei for Leiden. Implementeringsplanen
skal også beskrive nødvendig oppdatering av dependency-status og importtesten i
launcheren.

PyPI tilbyr et binærhjul som dekker 64-bit CPython 3.13 på Windows. Den
endelige låsfilen og importtesten skal likevel genereres og kjøres på nativ
Windows etter prosjektets vanlige låseflyt.

## Launcher og fremdrift

Eksisterende dialog utvides med algoritmen **Leiden**. Når den er valgt, vises:

- antall naboer, standard 20
- CPM-oppløsning
- dagens filterfelt
- dagens valg for å skjule «Ute av fokus»

Dialogen viser tre veiledende kombinasjoner, men begge tallfeltene kan alltid
endres manuelt: detaljerte grupper (`20`, `0.2`), mellomstore grupper (`50`,
`0.2`) og store grupper (`50`, `0.1`). Forslagene endrer ikke verdiene før
brukeren skriver dem inn.

Dialogen skal forklare kort at høyere `k` vanligvis gir en tettere graf, at
oppløsning påvirker detaljnivået. Validering skjer både i dialogen, workerens
argumentbehandling og domenemodellen.

Workerens eksisterende progresjonsmodell utvides med stadiene:

1. velger bilder
2. laster og validerer embeddings
3. finner nærmeste naboer
4. bygger graf
5. grupperer med Leiden
6. rangerer medlemmer
7. lagrer resultat

Det skal vises forløpt tid, men ikke en misvisende prosent eller forventet
sluttid. Native kall kan være lite avbrytbare mens de pågår; første leveranse
bruker samme kontrollerte prosessavbrudd og statusmodell som dagens algoritmer.

`TargetLock` holdes gjennom hele kjøringen i første leveranse, i samsvar med
dagens implementasjon. Å slippe låsen under beregning krever separat design for
revalidering av input før lagring og skal ikke innføres her.

## Webvisning

Dagens ruter og sikkerhetsmodell beholdes. Webendringene begrenses til å:

- vise «Leiden» som algoritmenavn
- vise de viktigste Leiden- og grafparameterne på run-panelet
- vise alle lagrede parametere på detaljsiden
- vise grafstatistikk og opprinnelige tellinger
- bruke eksisterende gruppekort og felles bildebrowser
- vise den samlede noise-gruppen som «Ugrupperte bilder»
- slette kjøringen gjennom eksisterende CSRF-beskyttede POST-flyt

Vanlig lokal read-only kan vise eksisterende resultater. Grupperingssider
forblir utilgjengelige i LAN-share. GET-oppslag skal ikke opprette eller migrere
OpenCLIP-databasen.

## Parameterutforsking etter første leveranse

Første leveranse lagrer nok informasjon til at brukeren kan kjøre flere
manuelle varianter og se resultatene som separate run-paneler. Den skal brukes
til å finne praktiske intervaller for `k`, likhetsterskel og CPM-oppløsning.

Etter denne utprøvingen kan en ny design vurdere:

1. valg av en tidligere Leiden-kjøring i launcher-dialogen for å fylle inn
   `selection_json` og parametere
2. «Kjør igjen» og «Kopier og endre» i launcheren
3. navngitte profiler med viste, redigerbare verdier
4. en begrenset parameterserie som fortsatt kjøres sekvensielt av launcheren
5. side-ved-side-visning av kjøringer med samme `input_fingerprint`
6. gjenbruk av en graf når inputfingerprint, grafbygger-versjon og alle
   grafparametere er identiske

Ingen av disse funksjonene skal startes fra webserveren. Hvis grafcache senere
innføres, må den være samlingslokal, regenererbar, versjonert og ha eksplisitt
livssyklus ved remove, unimport, sletting av runs og endrede embeddings.

## Ytelse og manuell kalibrering

Samlingen kan inneholde omtrent 20 000 bilder. Implementeringsplanen skal
estimere og måle minst:

- minne for embeddingmatrisen
- tid og toppminne for eksakt kNN med `k = 10`, `20` og `40`
- antall kanter for `union` og `mutual`
- tid og toppminne for grafobjektet og Leiden
- total kjøretid og størrelsen på databaseresultatet
- effekten av likhetsterskel og oppløsning på isolerte bilder og gruppestørrelser

Først brukes små syntetiske datasett og et ufarlig testutvalg. Før kjøring på
hovedsamlingen skal funksjonen prøves manuelt på Windows 11 med den låste
OpenCLIP-profilen. Målingen avgjør:

- foreløpig standard for CPM-oppløsning
- om `union` fortsatt er et godt standardvalg
- om grensen 1–200 for `k` er forsvarlig
- om eksakt kNN er praktisk for omtrent 20 000 bilder

Resultatet av kalibreringen dokumenteres før profiler eventuelt utformes.

## Tester

Implementeringsplanen skal dekke minst følgende tester.

### Parametere og graf

- ugyldig `k`, oppløsning, terskel, seed, nabomodus og vektmodus
- null eller ett gyldig bilde
- færre bilder enn `requested_k + 1`, inkludert lagret `effective_k`
- søkebildet fjernes korrekt ved dupliserte embeddings
- `union` gir kant når bare én retning velger naboen
- `mutual` krever begge retninger
- selvkanter og duplikate kanter opprettes ikke
- terskel og uvektet eller cosinusvektet graf gir forventede kanter
- grafstatistikk og inputfingerprint er deterministiske

### Leiden og resultat

- to tydelig adskilte tette delgrafer
- frakoblede komponenter
- isolerte noder lagres i én noise-gruppe
- singleton- og små Leiden-grupper beholdes
- alle gyldige file-ID-er finnes nøyaktig én gang i resultatet
- fast seed og identisk input gir samme medlemspartisjon
- representant og `center_rank` er deterministiske
- `membership_score` er `NULL`
- feil fra graf- eller Leiden-biblioteket gir feilet run uten delresultat

### Eksisterende livssyklus og grensesnitt

- manglende, ugyldige og avvikende embeddings håndteres som i dagens loader
- kontrollert avbrudd gir `cancelled` uten delresultat
- databasefeil under sluttlagring ruller tilbake grupper og medlemmer
- sletting av run påvirker ikke bilder, metadata, søk eller embeddings
- remove og unimport rydder Leiden-medlemskap etter eksisterende regler
- launcherkommandoen sender eksakte, validerte argumenter
- web viser Leiden-parametere, statistikk og noise-gruppe
- read-only migrerer aldri og LAN-share viser ikke grupperingssider
- MiniBatchKMeans og HDBSCAN fungerer uendret

### Migrering og avhengigheter

- OpenCLIP v2 migreres til v3 uten å endre eksisterende data
- migreringsfeil gir full rollback
- eldre runs uten Leiden-felter kan fortsatt vises
- dependency-status krever den komplette låste profilen
- importtest dekker den valgte graf- og Leiden-implementasjonen

## Krav til implementeringsplanen

Før kode endres, skal det lages en konkret implementeringsplan som:

1. beskriver eksisterende gjenbrukbar kode og nøyaktige filer som berøres
2. velger og verifiserer Leiden-biblioteket
3. spesifiserer parameterdataklasser og validert JSON-format
4. spesifiserer OpenCLIP v2–v3-migreringen med konkrete kolonner og constraints
5. beskriver grafbyggeren, fingerprint-formatet og deterministisk rangering
6. beskriver launcher-, worker- og webendringene uten å lage parallelle flyter
7. deler arbeidet i små steg med fokuserte tester etter hvert steg
8. angir den manuelle Windows- og 20 000-bilders verifikasjonen
9. skiller tydelig mellom automatiserte tester og empirisk parameterkalibrering

Planen skal ikke gjenåpne utsatte funksjoner uten å begrunne hvorfor de er
nødvendige for å teste Leiden. Sikkerheten til bildefilene og eksisterende
livssyklusregler går foran ytelsesoptimalisering og bekvemmelighetsfunksjoner.

# Bildegruppering med OpenCLIP

Dette dokumentet beskriver kontraktene som må bevares ved endringer i
bildegrupperingen. Historiske implementeringsplaner ligger i Git-historikken.

Grupperingen er implementert med MiniBatchKMeans, HDBSCAN og Leiden. Den er
dekket av automatiserte tester og er prøvd på Windows med omtrent 20 000
embeddings. Endringer i algoritmer eller standardverdier må måles på nytt mot
et representativt utvalg før de brukes på hovedsamlingen.

Relaterte dokumenter:

- `app-design.md` for produkt- og sikkerhetskontrakten
- `devel-docs/openclip-database.md` for sidecar-schema og migrering
- `devel-docs/launcher.md` for underprosesser og avbrudd
- `devel-docs/dependency-locks.md` for Windows-låsene

## Sikkerhetsgrense

En grupperingskjøring er regenererbare forslag i
`.bilder-openclip.sqlite3`. Den skal aldri endre:

- bildefiler eller avledede mediefiler
- `files` eller `file_sources`
- embeddings eller søkeresultater
- tagger, personer, steder eller kommentarer

En gruppe er bare et resultat i én bestemt kjøring, ikke en varig egenskap ved
bildet. Sletting av en kjøring skal bare fjerne den valgte run-raden, gruppene
og medlemskapene i OpenCLIP-databasen.

## Eierskap og flyt

- `bildebank/image_clustering.py` eier parametere, utvalg, embeddinglasting,
  algoritmer, run-livssyklus, lagring og sletting.
- `bildebank/openclip.py` eier schema, migrering og validering.
- `bildebank/launcher_commands.py` bygger den interne workerkommandoen.
- `bildebank/launcher_tools_tab.py` og `launcher_widgets.py` eier dialog og
  jobbstart.
- `bildebank/server_endpoints_clustering.py` eier read-only-visning og
  bekreftet sletting.
- `bildebank/server_browser_sources.py` og browserqueryene kobler en gruppe til
  den felles bildebrowseren.
- `bildebank/item_sidecars.py` og doctor eier livssyklus og integritetskontroll
  på tvers av hoved- og OpenCLIP-databasen.

Den tunge jobben startes bare fra **Verktøy**-fanen i launcheren. Den interne
CLI-kommandoen `_image-clustering-worker` er kun prosessgrensen for
`CommandRunner` og skal ikke dokumenteres som offentlig CLI. Webserveren kan
vise og eksplisitt slette resultater, men skal ikke starte, gjenta eller
avbryte grupperingsjobber.

NumPy, scikit-learn og igraph skal ikke importeres inn i den varige launcher-
eller serverprosessen når lazy import i worker-/domenekoden er tilstrekkelig.
Alle valgfrie avhengigheter installeres gjennom den komplette, låste
OpenCLIP-profilen; det skal ikke lages egne ulåste installasjonsveier.

## Utvalg og embeddings

Hoveddatabasens `files` har én kanonisk rad per SHA-256. `file_sources` er
importproveniens og kan ha flere rader for samme `files.id`. Kildebaserte
utvalg skal derfor bruke eksisterende `EXISTS`-semantikk og levere unike
`file_id`-er.

Utvalget er enten alle aktive filer eller et kanonisert filtersøk fra den
felles `BrowserSource`-flyten. `hide_out_of_focus` lagres eksplisitt i
`selection_json`; `is:deleted` avvises. Bare aktive stillbilder er
algoritmeinput. Videoer kan inngå i browserutvalget, men får ikke embedding og
grupperes ikke.

Embeddingene velges med eksakt `(file_id, model_name, pretrained)` og lastes
gjennom `load_validated_embedding_matrix()`. Loaderen skal:

- kontrollere positiv BLOB-lengde som er delelig med fire
- dekode `float32` og kreve endelige verdier, positiv norm og lik dimensjon
- kreve at embeddingens SHA-256 stemmer med aktiv `files.sha256`
- normalisere vektorene og sortere matrisen etter unik `file_id`
- telle gyldige, manglende og ugyldige embeddings separat
- hoppe over ugyldige rader uten å reparere, overskrive eller slette dem

Ingen gyldige embeddings gir en feilet run uten grupper. Parametrene til den
valgte algoritmen bestemmer eventuelle strengere minstekrav.

## Persistens og run-livssyklus

OpenCLIP-schema v3 bruker:

- `image_clustering_runs` for utvalg, modell, parametere, tellinger og status
- `image_clusters` for ordinære grupper og `kind='noise'`
- `image_cluster_members` for medlemskap og rangering

Medlemsmodellen håndhever unik `file_id` per run og at medlemsradens `run_id`
stemmer med gruppens run. `file_id` kan ikke ha foreign key til hoveddatabasen,
så livssyklus og doctor må håndheve koblingen eksplisitt. Schemaendringer skal
øke `OPENCLIP_SCHEMA_VERSION`, migrere additivt når mulig og aldri startes av
read-only-oppslag.

`run_image_clustering()` holder `TargetLock` gjennom hele jobben:

1. Eldre `running`-rader markeres `cancelled` av den nye skrivbare jobben.
2. En ny `running`-rad opprettes og committes.
3. Utvalg, embeddingtall og algoritmeparametere lagres.
4. Algoritmen kjører uten en lang SQLite-skrivetransaksjon.
5. Alle grupper og medlemmer lagres, og run settes `completed`, i én kort
   transaksjon.

Validerings- eller algoritmefeil gir `failed`; kontrollert avbrudd gir
`cancelled`. Begge beholder run-raden med en kort brukerrettet melding og uten
delvise grupper. Tekniske detaljer hører til launcherloggen. Feilede og
avbrutte kjøringer beholdes til eksplisitt sletting.

`selected_file_count` omfatter browserutvalget, `selected_image_count` de
kvalifiserte stillbildene, og embeddingtallene skiller gyldig, manglende og
ugyldig input. `clustered_file_count` og `actual_cluster_count` teller
ordinære grupper, ikke noise-medlemmer eller noise-gruppen.

## Felles resultatkontrakt

Alle algoritmeadaptere returnerer `ClusteringResult` med ordinære grupper og
eventuelt én noise-gruppe. Hvert vanlig medlem får `center_rank`, og laveste
aktive rang er representativt bilde. Lik avstand brytes med `file_id`.
Ordinære grupper sorteres etter opprinnelig medlemstall synkende og deretter
representantens `file_id`; den lagrede `display_order` beregnes ikke på nytt
når medlemmer senere forsvinner. Noise vises sist og sorteres etter `file_id`.

`actual_cluster_count` teller bare ordinære grupper. `algorithm_label` er
lokalt for én kjøring og skal aldri brukes som varig identitet eller
sammenlignes mellom runs.

### MiniBatchKMeans

Stabil algoritmenøkkel er `minibatch_kmeans`. Standardene i
`ClusteringParameters` er:

```text
n_clusters=20                 # bruker kan endre
random_seed=0
batch_size=1024
n_init=10
max_iter=100
reassignment_ratio=0.01
```

Antall gyldige embeddings må være minst `n_clusters`. Adapteren bruker
euklidsk avstand til sentroidet på L2-normaliserte embeddings. Færre ikke-tomme
grupper enn ønsket fullfører med advarsel og faktisk gruppetall.

### HDBSCAN

Stabil algoritmenøkkel er `hdbscan`. Standardene i `HdbscanParameters` er:

```text
min_cluster_size=5            # bruker kan endre
min_samples=None              # tomt følger min_cluster_size i minstekontrollen
metric=euclidean
cluster_selection_method=eom
cluster_selection_epsilon=0.0
alpha=1.0
leaf_size=40
allow_single_cluster=false
algorithm=auto
n_jobs=1
store_centers=medoid
copy=true
```

HDBSCAN velger antall grupper. Ordinære medlemmer rangeres med euklidsk
avstand til lagret medoid, og `probabilities_` lagres som `membership_score`.
Label `-1` lagres som én noise-gruppe med nullable senter og avstand.

### Leiden

Stabil algoritmenøkkel er `leiden`. Implementasjonen bruker `igraph==1.0.0`
og `Graph.community_leiden()` direkte. Standardene i `LeidenParameters` er:

```text
requested_k=20                # bruker kan endre, gyldig 1–200
neighbor_mode=union           # union eller mutual
minimum_similarity=0.0
weight_mode=cosine            # cosine eller unweighted
resolution=0.2                # bruker kan endre
random_seed=0
objective=CPM
n_iterations=-1              # til stabilt resultat
beta=0.01
```

Minst to embeddings kreves, og effektiv `k` er
`min(requested_k, n - 1)`. Eksakt cosinus-kNN beregnes deterministisk i
NumPy-chunks på 256 rader. Søkebildets egen matriserad fjernes eksplisitt, og
likhet brytes med `file_id`, slik at dupliserte embeddings håndteres stabilt.

Den rettede nabolisten gjøres til en enkel urettet graf. `union` beholder en
kant funnet i minst én retning; `mutual` krever begge. Kanter med ikke-positiv
likhet eller likhet under terskelen fjernes. `cosine` bruker positiv rå
cosinuslikhet som vekt; `unweighted` bruker vekt 1.

Isolerte noder sendes ikke til igraph og lagres samlet som noise. Ordinære
medlemmer rangeres med cosinusavstand til gruppens normaliserte
gjennomsnittsembedding. `membership_score` er `NULL`. En helt kantløs graf
fullfører med null ordinære grupper og alle bilder i noise.

Leiden-run lagrer grafstatistikk, bibliotekversjoner og inputfingerprint v1.
Fingerprinten er SHA-256 over modellnøkkel, dimensjon, sorterte `file_id`-er og
de normaliserte little-endian `float32`-radene. Den beviser lik algoritmeinput,
men er ikke filidentitet og erstatter ikke `selection_json` eller lagrede
parametere.

## Bildelivssyklus

- `remove` sletter embedding, søkeresultater og gruppemedlemskap for filen.
- `undelete` gjenoppretter ikke sidecar-data; `image-scan` må kjøres på nytt.
- `unimport` med gjenværende `file_sources` beholder medlemskapet.
- `unimport` av siste kilde fjerner medlemskapet i samme ATTACH-transaksjon
  som hoveddatabaseendringen.
- Run-raden og opprinnelige tellinger beholdes som historikk selv om alle
  aktive medlemmer forsvinner.

Tomme grupper kan ryddes, men en livssyklusoperasjon skal aldri slette en hel
run automatisk. En bekreftet run-sletting holder `TargetLock` og fjerner
medlemmer, grupper og den valgte run-raden uten å berøre andre OpenCLIP-data.

## Webvisning

Lokale GET-oppslag åpner eksisterende hoved- og OpenCLIP-database read-only og
skal aldri opprette, adoptere eller migrere schema. Resultater kan vises i
lokal read-only-modus, men alle grupperingssider er blokkert ved LAN-deling.
Sletting krever skrivbar server, CSRF-beskyttet POST, eksplisitt bekreftelse og
target-lås.

En gruppe vises gjennom vanlig `BrowserSource`, slik at navigasjon og
bildehandlinger ikke dupliseres. Gruppekort beregnes fra dagens aktive
medlemmer:

- representant og previews følger laveste tilgjengelige `center_rank`
- dato bruker `manual_date_from`/`manual_date_to` som yttergrenser og teller
  ukjent dato separat
- inntil tre vanlige tagger telles per distinkt medlemsfil
- personer teller bekreftede ansikter og manuelle person-fil-koblinger,
  deduplisert per person og fil; forslag telles ikke
- sted oppsummeres ikke særskilt

Full bildevisning kan vise ordinære medlemskap fra fullførte runs. Visningen
skjules når OpenCLIP er deaktivert eller serveren deler over LAN.

## Ved senere endringer

- Behandle parameterdataklassene og deres `as_dict()` som den stabile,
  persistente parameterkontrakten. Ikke stol på bibliotekstandarder.
- Bevar deterministisk sortering, tie-breaks, noise-semantikk og atomisk
  sluttlagring når en algoritme endres.
- Endringer i schema eller lagret JSON må ha migrerings-/kompatibilitetsplan og
  read-only-test som beviser at visning ikke skriver.
- Endringer i utvalg må gjenbruke `BrowserSource` og bevare mange-til-én-
  forholdet mellom `file_sources` og `files`.
- Endringer i algoritme, standardverdier, dimensjonsreduksjon eller tilnærmet
  nabosøk må benchmarkes på støttet Windows-installasjon og representativt
  datavolum. Transformasjon, seed og bibliotekversjon må lagres dersom de
  påvirker reproduksjon.
- Bevar lazy imports og den låste OpenCLIP-installasjonsprofilen.

Kjør minst disse fokuserte testene uten xdist:

```text
python -m pytest tests/test_image_clustering.py
python -m pytest tests/test_server_endpoints_clustering.py
python -m pytest tests/test_item_sidecar_lifecycle.py
python -m pytest tests/test_launcher_commands.py tests/test_launcher_tools_tab.py
```

Kjør deretter full suite med `python -m pytest -n auto`, samt Ruff og mypy når
produksjonskode er endret.

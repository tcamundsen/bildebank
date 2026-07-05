# Database v5: relative stier i bildesamlingen

Status: v5-migreringen regnes som fullført for støttede databaser. Gjeldende
kode antar at samlingsinterne bildestier allerede er relative, og inneholder
ikke lenger runtime-reparasjon av gamle absolutte `target_path`-verdier.

Dette dokumentet beskriver ønsket migrering fra absolutte stier til relative
stier for filer som ligger inne i en Bildebank-samling.

Målet er at en hel bildesamling skal kunne flyttes uten at databasen slutter å
fungere. Hvis samlingen for eksempel flyttes fra:

```text
C:\bilde-samling
```

til:

```text
C:\Users\brukernavn\minebilder
```

skal Bildebank fortsatt finne filene ved å tolke lagrede stier relativt til
mappen som inneholder `.bilder.sqlite3`.

Dette krever en eksplisitt databaseendring fra `schema_version=4` til
`schema_version=5`.

## Hovedregel

Stier til filer som ligger inne i bildesamlingen skal lagres relativt til
samlingsroten.

Eksempel:

```text
2024/01/IMG_001.jpg
deleted/2024/01/IMG_001.jpg
```

Runtime-kode som trenger faktisk filsti skal bygge den fra aktiv samlingsrot:

```text
target_root / relative_path
```

Databaseverdien skal ikke være avhengig av hvor samlingsmappen ligger på
maskinen akkurat nå.

## Stier som fortsatt skal være absolutte

Kilder ligger ofte utenfor bildesamlingen og flyttes ikke sammen med den.
Kildestier skal derfor fortsatt være absolutte:

- `sources.path`
- `sources.path_key`
- `file_sources.source_path`
- `file_sources.source_path_key`
- `errors.source_path` når verdien peker på en kildefil eller en ekstern feil

Dette gjør at Bildebank fortsatt kan kontrollere opprinnelige kilder når de
finnes, samtidig som selve samlingen kan flyttes.

## Hoveddatabase

I v5 skal disse feltene i `.bilder.sqlite3` bruke relative samlingsstier:

- `files.target_path`
- `files.target_path_key`
- `files.deleted_original_target_path`

`files.target_path` skal lagre relativ sti med `/` som skilletegn i databasen,
uavhengig av operativsystem.

`files.target_path_key` skal være en normalisert relativ nøkkel. Den skal ikke
baseres på `Path.resolve()` eller absolutt plassering på disk.

`files.deleted_original_target_path` skal også lagres relativt når verdien
peker på filens tidligere plassering inne i samlingen.

`meta.target_path` skal ikke være autoritativ plassering for samlingen etter
v5. Aktiv samlingsrot er mappen som inneholder `.bilder.sqlite3`.

`collection_id` beholdes som stabil identitet for den logiske samlingen.

## Side-databaser

Ansiktsdatabasen `.bilder-faces.sqlite3` lagrer også samlingsinterne stier.
Disse må følge samme relative modell:

- `scanned_files.target_path`
- `scanned_files.target_path_key`
- `faces.target_path_key`

OpenCLIP-databasen `.bilder-openclip.sqlite3` må også oppdateres:

- `image_embeddings.target_path`
- `image_embeddings.target_path_key`
- `image_search_results.target_path`
- `image_search_results.target_path_key`

Hvis side-databaser ikke migreres samtidig med hoveddatabasen, må de enten
merkes som utdaterte eller slettes/regenereres trygt. Face-scan og OpenCLIP
scan kan gjenskapes, men de kan være tidkrevende. Derfor bør migrering
foretrekkes når dataene er konsistente.

## Migrering fra v4 til v5

Migreringen skal være eksplisitt og kjøres med:

```powershell
bildebank migrate
```

Den skal følge samme sikkerhetsmodell som tidligere migreringer:

- ta backup før endring
- ta target lock
- kjøre hoveddatabaseendringer i én transaksjon
- stoppe på uventet struktur eller inkonsistente data
- kjøre `PRAGMA foreign_key_check`
- kjøre `PRAGMA integrity_check`
- først sette `schema_version=5` når migreringen er validert

Før stier konverteres skal migreringen validere at alle
`files.target_path`-verdier som skal bli relative faktisk ligger under aktiv
samlingsrot.

Hvis en `files.target_path` peker utenfor samlingsrot, skal migreringen stoppe
med en tydelig feil. Den skal ikke gjette, flytte filer eller skrive om stier
som ikke kan forklares trygt.

For hver samlingsintern absolutt sti skal migreringen:

- beregne relativ sti fra aktiv samlingsrot
- lagre relativ sti med `/`
- beregne ny relativ `target_path_key`
- la hash, størrelse, dato, `stored_filename`, `original_filename` og
  provenance være uendret

Kildestier skal ikke konverteres.

## Runtime-regler etter v5

Kode som leser en importert fil fra databasen må tolke `files.target_path` som relativ
sti og kombinere den med aktiv samlingsrot.

Kode som skriver nye `files`-rader må lagre relativ sti i databasen.

Kode som sammenligner lagrede filstier må bruke relativ path key. Absolutt
`resolve()` skal ikke brukes for samlingsinterne database-nøkler.

Kode som viser stier til brukeren kan vise relative stier når det er mest
lesbart, eller absolutte stier når det trengs for feilsøking. Dette er
visningslogikk og skal ikke styre databaseformatet.

## Tester som trengs

Minimumstester for v5:

- Ny database lagrer `files.target_path` relativt etter import.
- v4-database med absolutte `files.target_path` under samlingsrot migreres til
  relative stier.
- Etter migrering kan samlingsmappen flyttes, og vanlige kommandoer finner
  filene via ny samlingsrot.
- Migrering stopper hvis en `files.target_path` peker utenfor samlingsrot.
- `sources.path` og `file_sources.source_path` forblir absolutte etter
  migrering.
- `remove` og `unimport` fungerer med relative lagrede filstier.
- `make-browser`, face-visninger og OpenCLIP-søk fungerer etter flytting av
  samlingen.
- Face/OpenCLIP-data migreres til relative stier eller håndteres tydelig som
  utdaterte/regenererbare.

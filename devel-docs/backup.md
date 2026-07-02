# Bildebank backup-API

Dette dokumentet beskriver utviklerkontrakten for:

```bash
bildebank backup DEST
bildebank backup --adopt DEST
```

Brukerdokumentasjonen ligger i `docs/backup.md`.

## Implementert hovedregel

`DEST` tolkes alltid som en backup-plassering, altså foreldremappen der
backupen skal ligge. Selve backupmappen får alltid samme navn som aktiv
bildesamlingsmappe.

```text
source_dir      = aktiv bildesamlingsmappe
source_name     = source_dir.name
backup_parent   = DEST
backup_dir      = backup_parent / source_name
```

Eksempel:

```bash
bildebank backup /media/tom/Backup
```

Hvis aktiv samling heter `bilde-samling`, blir backupmålet:

```text
/media/tom/Backup/bilde-samling
```

Programmet støtter ikke fritt valgt navn på backupmappen. Hvis brukeren kjører:

```bash
bildebank backup /media/tom/Backup/mitt-navn
```

tolkes `mitt-navn` som foreldremappe, og backupmålet blir:

```text
/media/tom/Backup/mitt-navn/<aktiv-samlingsnavn>
```

## Sikkerhetssjekker

Før speiling skal kommandoen validere:

1. Aktiv mappe er en gyldig Bildebank-samling.
2. `backup_parent` finnes.
3. `backup_parent` er en mappe.
4. `backup_dir` beregnes som `backup_parent / source_dir.name`.
5. `backup_dir` er ikke samme mappe som `source_dir`.
6. `backup_dir` er ikke en overmappe til `source_dir`.
7. `backup_dir` ligger ikke inne i `source_dir`.
8. Hvis `backup_dir` finnes, må den være merket som Bildebank-backup.
9. Eksisterende backup må ha `backup_of` som matcher aktiv `collection_id`.

Hvis `backup_parent` ikke finnes, skal kommandoen avbryte. Første versjon skal
ikke automatisk opprette en lang foreldresti.

Hvis `backup_dir` finnes uten `.bildebank-backup.json`, skal kommandoen avbryte
for å hindre speiling inn i en vanlig mappe.

`backup --adopt` er en eksplisitt reparasjonsflyt for eksisterende backupmapper
der `.bildebank-backup.json` mangler, eller der metadatafilen finnes men
`backup_of` mangler eller er tom. Kommandoen skal ikke speile filer.

## Collection ID

Hver samling har en stabil UUID i hoveddatabasen:

```text
meta.collection_id
```

Denne ID-en identifiserer den logiske samlingen og skal ikke endres bare fordi
samlingen flyttes.

Backupmetadata bruker denne verdien for å kontrollere at en eksisterende backup
hører til riktig samling.

## Backupmetadata

Backupmappen inneholder:

```text
.bildebank-backup.json
```

Minimumskontrakt:

```json
{
  "backup_of": "collection-id",
  "source_name": "bilde-samling",
  "created_by": "bildebank",
  "format_version": 1,
  "status": "complete"
}
```

`backup_of` må være lik aktiv `collection_id`.

Metadatafilen finnes bare i backupen, ikke i kildesamlingen. Den skal derfor
ekskluderes fra speilingen.

Adopsjon/reparasjon skriver samme metadatafil med `status: "adopted"`. Neste
vanlige backupkjøring vil skrive `status: "in-progress"` og deretter
`status: "complete"` hvis speilingen lykkes.

## Metadata-status

Før speiling starter, skal programmet skrive metadata med:

```json
{
  "status": "in-progress"
}
```

Etter vellykket speiling skal metadata oppdateres til:

```json
{
  "status": "complete"
}
```

Hvis backupen avbrytes eller speilingsverktøyet feiler, blir metadata stående
som `in-progress`. Neste kjøring kan fortsatt godta mappen hvis `backup_of`
matcher aktiv samling, fordi ny speiling skal konvergere mot riktig tilstand.

## Låsing

Reell backup skal ta `TargetLock` på aktiv bildesamling. Låsen skal holdes fra
før `collection_id` eventuelt opprettes og til speilingen er ferdig og metadata
er oppdatert til `complete`.

Dette hindrer andre Bildebank-kommandoer som respekterer target-lock fra å
endre databaser eller bildefiler mens backupen kopierer samlingen.

`--dry-run` skal ikke ta target-lock. Dry-run skal fortsatt ikke opprette
backupmappe, metadata eller `collection_id`.

Hvis prosessen avbrytes normalt, for eksempel med `Ctrl+C`, skal context
manageren fjerne `.bildebank.lock`. Hvis PC-en krasjer eller prosessen drepes
hardt, kan lockfilen bli liggende igjen. Den skal ikke fjernes automatisk av
programmet.

## Speilingsmotor

Sikkerhetssjekker og metadata håndteres i Python. Selve filspeilingen velger
plattformmotor slik:

```text
Windows:       robocopy hvis tilgjengelig
Linux/macOS:   rsync hvis tilgjengelig
Fallback:      ren Python-speiling
```

Hvis `robocopy` eller `rsync` mangler, skal brukeren varsles tydelig om at
fallback brukes:

```text
ADVARSEL: robocopy/rsync mangler. Bruker tregere Python-kopiering.
```

## Robocopy

På Windows brukes `robocopy` med speiling:

```powershell
robocopy SOURCE BACKUP_DIR /MIR /Z /DCOPY:DAT /COPY:DAT /R:2 /W:5 /XJ /FFT /XF .bildebank-backup.json .bildebank.lock .bildebank.log
```

Viktige krav:

- `/MIR` brukes for at backupen skal speile aktiv samling.
- `/XF .bildebank-backup.json .bildebank.lock .bildebank.log` må være med.
  Backupmetadataen skal bevares i backupen, mens lock- og loggfiler er
  runtime-filer som ikke skal speiles.
- Exitkoder `0` til og med `7` regnes som suksess.
- Exitkoder over `7` regnes som feil.

## Rsync

På Linux og macOS brukes `rsync` med speiling:

```bash
rsync --progress --stats -a --delete --exclude .bildebank-backup.json --exclude .bildebank.lock --exclude .bildebank.log SOURCE/ BACKUP_DIR/
```

Viktige krav:

- Kildeargumentet må ha trailing slash slik at innholdet i samlingen speiles inn
  i `backup_dir`.
- `--delete` brukes for at backupen skal speile aktiv samling.
- `--exclude .bildebank-backup.json`, `--exclude .bildebank.lock` og
  `--exclude .bildebank.log` må være med. Backupmetadataen skal bevares i
  backupen, mens lock- og loggfiler er runtime-filer som ikke skal speiles.
- Exitkode `0` regnes som suksess.

## Python-fallback

Python-fallbacken skal:

- kopiere nye og endrede filer med `shutil.copy2`
- slette filer og mapper i backupen som ikke finnes i kilden
- aldri slette noe i kildesamlingen
- bevare `.bildebank-backup.json` i backupen
- ikke kopiere `.bildebank.lock` eller `.bildebank.log`
- slette gamle `.bildebank.lock` og `.bildebank.log` fra backupen
- ta med hele samlingen, inkludert databaser, HTML-filer og `deleted/`

Fallbacken er primært for portabilitet og testbarhet. For store samlinger er
`robocopy` og `rsync` forventet å være raskere og mer robuste.

## Dry-run

`--dry-run` validerer målet og viser om kommandoen ville opprette eller
oppdatere backupen, men den kopierer ikke, sletter ikke og skriver ikke
metadata.

Hvis ekstern motor finnes, skal dry-run kjøre samme speilingskommando i
tørrkjøringsmodus:

- Windows: `robocopy` med `/L`
- Linux/macOS: `rsync` med `--dry-run`

Hvis `robocopy` eller `rsync` mangler, viser Python-fallbacken bare planen.
En ny backup kan ikke opprettes inni en eksisterende Bildebank-backup.

Eksempel:

```text
Source:
  /home/tom/bilde-samling

Backup parent:
  /media/tom/Backup

Backup directory:
  /media/tom/Backup/bilde-samling

Mode:
  Dry run

Result:
  Would update backup.
  motor=rsync
```

## Adopsjon av eksisterende backup

`bildebank backup --adopt DEST` bruker samme `DEST`-tolkning som vanlig backup:

```text
backup_dir = DEST / source_dir.name
```

Kommandoen er bare tillatt når `backup_dir` finnes og én av disse er sann:

- `.bildebank-backup.json` mangler.
- `.bildebank-backup.json` finnes, men `backup_of` mangler eller er tom.

Kommandoen skal avvise ugyldig JSON, `backup_of` som matcher aktiv samling
(adopsjon er unødvendig), og `backup_of` som peker på en annen samling.

Før brukeren bekrefter, skal kommandoen vise:

- aktiv bildesamling
- backup parent
- beregnet backupmappe
- metadata-status
- aktiv `collection_id`
- antall databaseførte filer i `files`
- antall og prosent som finnes i backup med samme relative sti og størrelse
- antall som mangler
- antall som finnes, men ikke er vanlig fil eller har feil størrelse
- antall ekstra støttede mediafiler i backupen som ikke finnes i `files.target_path`

Sammenligningen bruker `files.target_path` og `files.size_bytes`, ikke full
SHA-256. Både aktive og slettede filer telles, siden backup skal inkludere
`deleted/`. Ekstra filer telles bare når de er støttede mediafiler.

`--dry-run` skal vise samme rapport, men ikke spørre om bekreftelse og ikke
skrive metadata.

Uten `--dry-run` skal brukeren skrive nøyaktig:

```text
registrer backup
```

Hvis teksten ikke matcher, skal kommandoen avbryte uten endringer.

Ved bekreftet adopsjon skal programmet opprette eller oppdatere
`.bildebank-backup.json`, bevare ufarlige eksisterende felt, og sette/oppdatere:

- `backup_of`
- `source_name`
- `created_by`
- `bildebank_version`
- `format_version`
- `status`
- `updated_at`
- `adopted_at`

Adopsjon skal ikke starte speiling. Brukeren må kjøre vanlig
`bildebank backup DEST` etterpå for å oppdatere backupinnholdet.

## Feilhåndtering

Hvis eksisterende `backup_dir` mangler metadata:

```text
Kan ikke lage backup.

Målmappen finnes allerede, men ser ikke ut til å være en bildebank-backup:

  <backup_dir>

Velg en annen backup-plassering, eller flytt/gi nytt navn til denne mappen.
```

Hvis metadata finnes, men `backup_of` ikke matcher aktiv `collection_id`, skal
kommandoen avbryte uten speiling.

Hvis ekstern speilingsmotor feiler etter at `in-progress` er skrevet, skal
metadatafilen bli liggende med `status: "in-progress"`.

## Ikke implementert ennå

Følgende sikkerhetsidé fra opprinnelig design er ikke implementert:

- lagre `last_working_path`
- lagre `last_machine_name`
- advare når samme `collection_id` brukes fra en ny sti eller maskin
- tilby valg mellom "samme samling flyttet" og "ny uavhengig kopi"

Bakgrunnen er at en manuell kopi av hele samlingsmappen også kopierer
`collection_id`. Programmet kan derfor ikke perfekt vite om samlingen er
flyttet eller kopiert. En fremtidig løsning kan likevel oppdage vanlige feil ved
å lagre siste arbeidssted og maskinnavn.

# Bildebank backup-API

Dette dokumentet beskriver ønsket oppførsel for kommandoen:

```powershell
bildebank backup DEST
```

Målet er å lage en enkel og trygg backup-funksjon for en lokal bildesamling. Programmet `bildebank` kjøres fra katalogen som inneholder bildesamlingen og databasefilen.

Eksempel på aktiv samling:

```text
C:\Users\Tom\bilde-samling
```

I eksemplene under er samlingsnavnet derfor:

```text
bilde-samling
```

## Hovedregel

Argumentet `DEST` skal alltid tolkes som en **backup-plassering**, altså en foreldremappe der backupen skal ligge.

Selve backupmappen skal alltid få samme navn som katalogen bildesamlingen ligger i.

Programmet skal ikke støtte at brukeren selv velger et annet navn på backupmappen.

Det vil si:

```powershell
bildebank backup F:\
```

skal bruke backupmål:

```text
F:\bilde-samling
```

Og:

```powershell
bildebank backup F:\mappenavn\submappe
```

skal bruke backupmål:

```text
F:\mappenavn\submappe\bilde-samling
```

Dette gjør kommandoen enkel: brukeren velger bare hvor backup-roten skal ligge, ikke hva backupmappen skal hete.

## Terminologi

```text
source_dir      = katalogen bildebank kjøres fra
source_name     = navnet på source_dir
backup_parent   = argumentet DEST
backup_dir      = backup_parent / source_name
```

Eksempel:

```text
source_dir    = C:\Users\Tom\bilde-samling
source_name   = bilde-samling
backup_parent = F:\
backup_dir    = F:\bilde-samling
```

## Ønsket kommandoform

Vanlig backup til ekstern disk:

```powershell
bildebank backup F:\
```

Backup til en undermappe på ekstern disk:

```powershell
bildebank backup F:\Backup
```

Dette gir:

```text
F:\Backup\bilde-samling
```

Backup til dypere undermappe:

```powershell
bildebank backup F:\mappenavn\submappe
```

Dette gir:

```text
F:\mappenavn\submappe\bilde-samling
```

## Bevisst begrensning

Dette skal ikke være støttet som alternativt backupnavn:

```powershell
bildebank backup F:\mitt-andre-navn
```

Kommandoen skal i stedet tolke `F:\mitt-andre-navn` som foreldremappe og bruke:

```text
F:\mitt-andre-navn\bilde-samling
```

Hvis brukeren vil organisere backupene annerledes, må han lage en passende foreldremappe.

Eksempel:

```powershell
bildebank backup F:\Backup\Familiebilder
```

Gir:

```text
F:\Backup\Familiebilder\bilde-samling
```

## Sjekk av backupmål

Når `backup_dir` er beregnet, skal programmet gjøre disse sjekkene:

```text
Hvis backup_parent ikke finnes:
    Avbryt med feilmelding.

Hvis backup_dir ikke finnes:
    Opprett ny backup i backup_dir.

Hvis backup_dir finnes og er gyldig backup av samme bildesamling:
    Oppdater eksisterende backup.

Hvis backup_dir finnes, men ikke er en gyldig backup av samme bildesamling:
    Avbryt med feilmelding.
```

## Metadata for samlingen

Hver bildesamling bør ha en stabil UUID som identifiserer den logiske samlingen.

Denne kan ligge i databasen, eventuelt i en metadatafil. Siden bildebank allerede bruker database, er databasen sannsynligvis mest naturlig.

Minimum:

```text
collection_id = UUID
```

Eksempel:

```text
collection_id = 7f6b5c5c-8e9a-4a0d-b34d-9d9d3f51b6c1
```

Denne ID-en skal ikke endres bare fordi samlingen flyttes.

## Metadata for backupen

Backupmappen bør inneholde en liten metadatafil, for eksempel:

```text
.bildebank-backup.json
```

Eksempel:

```json
{
  "backup_of": "7f6b5c5c-8e9a-4a0d-b34d-9d9d3f51b6c1",
  "source_name": "bilde-samling",
  "created_by": "bildebank",
  "format_version": 1
}
```

`backup_of` skal peke til `collection_id` for samlingen backupen tilhører.

## Gyldig eksisterende backup

En eksisterende `backup_dir` skal regnes som gyldig backup hvis:

```text
backup_dir finnes
backup_dir inneholder .bildebank-backup.json
.bildebank-backup.json har backup_of som matcher collection_id for aktiv samling
```

Hvis `backup_of` ikke matcher aktiv `collection_id`, skal kommandoen avbrytes.

Hvis `backup_dir` finnes, men mangler `.bildebank-backup.json`, skal kommandoen også avbrytes.

Dette hindrer at `bildebank backup` ved en feil syncer inn i en vanlig mappe som tilfeldigvis har samme navn som samlingen.

## Eksempel: ny backup

Aktiv samling:

```text
C:\Users\Tom\bilde-samling
```

Kommando:

```powershell
bildebank backup F:\
```

Hvis dette ikke finnes:

```text
F:\bilde-samling
```

skal programmet opprette:

```text
F:\bilde-samling
F:\bilde-samling\.bildebank-backup.json
```

og deretter kopiere/synkronisere innholdet fra samlingen.

## Eksempel: oppdater eksisterende backup

Aktiv samling:

```text
C:\Users\Tom\bilde-samling
```

Kommando:

```powershell
bildebank backup F:\
```

Hvis dette finnes:

```text
F:\bilde-samling\.bildebank-backup.json
```

og metadatafilen inneholder riktig `backup_of`, skal backupen oppdateres.

## Eksempel: konflikt

Aktiv samling:

```text
C:\Users\Tom\bilde-samling
```

Kommando:

```powershell
bildebank backup F:\
```

Hvis denne mappen finnes:

```text
F:\bilde-samling
```

men mappen ikke inneholder gyldig `.bildebank-backup.json`, skal kommandoen avbrytes.

Eksempel på feilmelding:

```text
Kan ikke lage backup.

Målmappen finnes allerede, men ser ikke ut til å være en bildebank-backup:

  F:\bilde-samling

Velg en annen backup-plassering, eller flytt/gi nytt navn til denne mappen.
```

## Sjekk mot flyttet eller kopiert samling

Programmet bør lagre hvor samlingen sist ble brukt fra.

Forslag til metadata i databasen:

```text
collection_id
last_working_path
last_machine_name
last_seen_at
```

Når `bildebank` startes, eller i hvert fall før kommandoer som skriver data, kan programmet sammenligne:

```text
nåværende arbeidsmappe mot last_working_path
nåværende maskinnavn mot last_machine_name
```

Hvis dette er endret, bør programmet advare brukeren.

Eksempel:

```text
Denne bildesamlingen ser ut til å ha blitt åpnet fra et nytt sted.

Sist brukt fra:
  C:\Users\Tom\bilde-samling

Nå brukt fra:
  D:\bilde-samling

Hvis du har flyttet samlingen, er dette normalt.

Hvis du har kopiert samlingen, bør du ikke bruke begge kopiene som arbeidskopier.
Det kan føre til at backupen senere oppdateres fra feil kopi.
```

Mulige valg:

```text
1. Dette er samme samling, bare flyttet
2. Dette er en kopi som skal bli en ny uavhengig samling
3. Avbryt
```

Ved valg 1:

```text
Behold collection_id.
Oppdater last_working_path og last_machine_name.
```

Ved valg 2:

```text
Lag ny collection_id.
Nullstill eventuell backup-tilknytning.
Oppdater last_working_path og last_machine_name.
```

Ved valg 3:

```text
Avbryt uten endringer.
```

Dette er en beskyttelse mot at brukeren manuelt kopierer hele samlingen, lar to kopier utvikle seg hver for seg, og deretter forsøker å bruke samme backupmål for begge.

## Viktig begrensning

Hvis brukeren manuelt kopierer hele samlingsmappen, kopieres også metadata og `collection_id`.

Programmet kan derfor ikke perfekt vite om samlingen er flyttet eller kopiert.

Men ved å lagre `last_working_path` og `last_machine_name` kan programmet oppdage at samlingen brukes fra et nytt sted og advare før backup eller andre skriveoperasjoner.

Dette er ikke en full synkroniseringsløsning mellom flere arbeidskopier. Det er en sikkerhetsmekanisme for å hindre vanlige brukerfeil.

## Dry-run

Backupkommandoen bør støtte:

```powershell
bildebank backup F:\ --dry-run
```

Dry-run skal vise hva som ville blitt gjort, uten å kopiere, slette eller endre filer.

Eksempel:

```text
Source:
  C:\Users\Tom\bilde-samling

Backup parent:
  F:\

Backup directory:
  F:\bilde-samling

Mode:
  Dry run

Result:
  Existing backup found. Would update backup.
```

## Foreldremappe som ikke finnes

Hvis brukeren kjører:

```powershell
bildebank backup F:\mappenavn\submappe
```

og denne foreldremappen ikke finnes:

```text
F:\mappenavn\submappe
```

bør kommandoen i første versjon avbryte med feilmelding.

Eksempel:

```text
Backup-plasseringen finnes ikke:

  F:\mappenavn\submappe

Opprett mappen først, eller velg en eksisterende plassering.
```

Det er tryggere enn at programmet automatisk oppretter en lang sti etter en mulig skrivefeil.

Et eventuelt senere valg kan være å støtte:

```powershell
bildebank backup F:\mappenavn\submappe --create-parent
```

men dette er ikke nødvendig i første versjon.

## Robocopy / synkronisering

På Windows kan selve filsynkroniseringen gjøres med `robocopy`.

Forsiktig standard kan være noe i denne retningen:

```powershell
robocopy SOURCE BACKUP_DIR /MIR /Z /DCOPY:DAT /COPY:DAT /R:2 /W:5 /XJ /FFT
```

Dry-run kan bruke `/L`:

```powershell
robocopy SOURCE BACKUP_DIR /MIR /Z /DCOPY:DAT /COPY:DAT /R:2 /W:5 /XJ /FFT /L
```

Et er vurder om backupen skal speile med `/MIR` eller bruke `/E`, og valget har 
falt på `/MIR`. Det må sjekkes nøye at backup skjer til riktig mappe.

## Oppsummering av ønsket API

```powershell
bildebank backup F:\
```

lager eller oppdaterer:

```text
F:\bilde-samling
```

```powershell
bildebank backup F:\mappenavn\submappe
```

lager eller oppdaterer:

```text
F:\mappenavn\submappe\bilde-samling
```

Programmet skal:

```text
- alltid bruke navnet på aktiv samlingsmappe som backupmappenavn
- ikke støtte fritt valgt backupmappenavn
- avbryte hvis beregnet backupmappe finnes, men ikke er gyldig backup
- bruke collection_id og backup_of for å sjekke at backupen hører til riktig samling
- advare hvis samlingen brukes fra en annen sti eller maskin enn sist
- støtte --dry-run
```

Sikkerhetssjekk som må gjøres:
1. Programmet står i en gyldig bildebank-samling.
2. Kilden er samlingsroten, ikke en undermappe.
3. Backupmålet beregnes som DEST\<source-folder-name>.
4. Hvis backupmålet finnes, må det være merket som bildebank-backup.
5. backup_of må være lik collection_id.
6. Backupmålet må ikke være lik kilden.
7. Backupmålet må ikke være en overmappe til kilden.
8. Kilden må ikke være en overmappe til backupmålet.

# Windows-pilot for snapshots

Denne sjekklisten skal gjennomføres på Windows 11 før første versjon regnes som
ferdig. Bruk en liten testsamling og en ekstern lagringsenhet av samme type som
senere skal brukes til snapshots. Ingen test skal kjøres mot den eneste kopien
av bilder.

## Testoppsett

Registrer:

- dato og Bildebank-versjon/commit
- Windows-versjon
- type eksternt medium og filsystem, for eksempel NTFS, exFAT eller FAT32
- samlingssti og eksakt repositorysti
- antall filer og byte før testen

Testsamlingen skal inneholde:

- minst ti vanlige bilder fordelt på flere undermapper
- minst én fil under `deleted\`
- minst én ukjent vanlig fil som ikke finnes i `files`
- thumbnails og generert HTML som skal utelates
- hoveddatabase, OpenCLIP-database og minst én face-database når funksjonene er
  i bruk
- minst to filer med identisk innhold
- filnavn med mellomrom og norske tegn

Lag en uavhengig liste med SHA-256 og størrelse rett før snapshotet som senere
skal restore-testes. Kjør fra rotmappen til bildebank-koden:

```powershell
.\tools\snapshot-media-hashes.ps1 `
  -Collection ..\testsamling2 `
  -Output ..\testsamling2-media-fasit.csv
```

Listen er bare testfasit og skal ligge utenfor både bildesamlingen og
repositoryet. Skriptet tar med støttede mediefiler under blant annet `deleted\`,
men utelater `thumbs\`. Det nekter å overskrive en eksisterende liste uten
`-Force`.

## Oppretting og inkrementelle snapshots

1. Kjør `snapshot create --dry-run` mot en manglende repositorymappe.
2. Kontroller at dry-run ikke oppretter mappe, lås eller metadata.
3. Kontroller inventar, eksklusjoner, plassestimat og repositoryplassering.
4. Opprett første snapshot og krev status `complete`.
5. Kjør `snapshot list`, `snapshot problems`, rask `snapshot check` og
   `snapshot check --full`.
6. Kontroller at thumbnails og generert HTML ikke finnes som snapshotposter,
   og at `deleted\` og den ukjente filen finnes.
7. Legg til ett bilde, flytt ett bilde med Bildebank og marker ett annet som
   fjernet uten fysisk sletting.
8. Opprett et nytt snapshot og kontroller at det gamle snapshotet fortsatt har
   identiske manifestfiler og fortsatt kan kontrolleres.
9. Opprett et tredje snapshot uten kildeendringer og kontroller forventet
   objektgjenbruk.

### Logg fra Tom Cato

ADVARSEL: Ufullstendig kjøring 73acd157-a000-4150-9385-d74a1c7ee7a2: 69.2 KB, alder 10m 05s

Vi fikk en ufullstendig kjøring første gang fordi vi hadde en bug med manglende O_BINARY. Det
oppstår ikke ufullstendige kjøringer ved ny snapshot etter bugfix.

## Avbrudd og sikkerhetsgrenser

Den ordinære pytest-suiten dekker deterministisk avbrudd under objektkopiering,
simulert fullt medium, bevaring og rapportering av `incomplete\`, frigjøring og
bevaring av låser, full kontroll av tidligere snapshots, ikke-tom
repositorymappe, symbolske lenker, Windows-junction og simulert FAT32-grense.

Utfør bare disse gjenværende kontrollene, og bare på testdata:

1. Avbryt én reell snapshotkjøring med Ctrl+C under kopiering på Windows.
   Legg først til en ny testfil med innhold som ikke allerede finnes i
   repositoryet, slik at kjøringen faktisk må kopiere et nytt objekt.
   Kontroller at tidligere publiserte snapshots fortsatt består full kontroll,
   at ingen ny snapshotmappe ble publisert, og at eventuell `incomplete\` blir
   rapportert og bevart. En kontrollert Ctrl+C skal normalt frigjøre låsene.
2. Kjør den automatiske Windows-testen for junction fra rotmappen til
   bildebank-koden:

   ```powershell
   .venv\Scripts\python.exe -m pytest tests/test_snapshot_cli.py::SnapshotCliTests::test_snapshot_dry_run_rejects_windows_junction_before_repository_write
   ```

   En junction er en Windows-mappe som peker videre til en annen mappe. Hvis
   testen viser `PASSED`, er kontrollen ferdig og du skal ikke teste dette
   manuelt. Hvis den viser `SKIPPED` med beskjed om at junction ikke kunne
   opprettes, noter at denne kontrollen ikke kunne utføres på maskinen.
3. Ingen manuell FAT32-kontroll utføres i denne piloten. Pytest dekker
   størrelsesgrensen med en simulert test. En kontroll på et fysisk FAT32-medium
   er bare aktuell senere dersom FAT32 faktisk skal støttes som backupmedium.
   Ikke opprett en stor fil eller fyll en disk for å fremtvinge testen.

Ikke fremprovoser et virkelig fullt vanlig medium. Pytest simulerer `ENOSPC` og
kontrollerer at ingen ny snapshotmappe publiseres, at tidligere snapshots er
uskadet, at låser frigjøres og at `incomplete\` bevares.

## Hel restore

1. Kjør hel restore med `--dry-run` til en manglende mappe.
2. Kontroller at dry-run ikke oppretter mål eller staging.
3. Kjør reell restore og bruk den eksakte tekstbekreftelsen.
4. Kjør `doctor --deep` mot restorekopien.
5. Sammenlign alle mediefiler mot den uavhengige SHA-256-listen:

   ```powershell
   .\tools\snapshot-media-hashes.ps1 `
     -Collection ..\restored-dir `
     -Output ..\restored-dir-media.csv `
     -CompareWith ..\testsamling2-media-fasit.csv
   ```

   Forvent `Sammenligning bestått` og `Avvik: 0`.
6. Kontroller hoveddatabase, OpenCLIP, face-database, ukjent fil og `deleted\`.
7. Kontroller at thumbnails og generert HTML kan regenereres.
8. Kjør samme restore på nytt mot den nå ikke-tomme målmappen. Den skal avvises
   uten å endre noen fil.
9. Test også en eksisterende tom målmappe.
10. Avbryt en restore under kopiering. Staging skal bevares, målmappen skal ikke
    publiseres, og neste forsøk skal rapportere restene.

Ikke bruk originalen og restorekopien parallelt som uavhengige samlinger. De
skal ha samme `collection_id`.

## Restore av enkeltfil
1. Kjør dry-run og reell restore med `--path` til en ny eksportmappe.
2. Sammenlign eksportfilens SHA-256 og størrelse med testfasiten.
3. Kontroller filens endringstid innenfor målfilsystemets tidsoppløsning.
4. Kjør samme kommando på nytt; den eksisterende filen skal ikke overskrives.
5. Kjør restore-testsuiten fra rotmappen til bildebank-koden:

   ```powershell
   python -m pytest tests\test_snapshot_restore.py
   ```

   Testsuiten lager et isolert `degraded` snapshot, krever valg av variant og
   eksporterer både `expected` og `observed` med kontroll av byteinnholdet. Dette
   skal ikke fremprovoseres manuelt i testsamlingen.
6. Testsuiten kontrollerer hash-suffikset på den eksporterte
   `observed`-varianten.
7. Testsuiten lager også en isolert `recovery_only`-post, finner `entry_id` via
   problemlisten og eksporterer posten med `entry_id`.
8. Testsuiten avbryter enkeltfil-restore deterministisk etter at de første
   bytene er skrevet. Den kontrollerer at delfilen bevares, at repositorylåsen
   frigjøres, og at neste forsøk nekter å overskrive delfilen. Ikke forsøk å
   treffe kopieringen manuelt med Ctrl+C.

Hele dette avsnittet er testet og godkjent.

## Diskrotasjon

1. Gjenta oppretting og full kontroll på minst to separate medier.
2. Koble fra hvert medium etter bruk.
3. Kontroller at ett medium kan brukes til liste, full kontroll og restore på
   en annen Windows 11-maskin med samme Bildebank-versjon.
4. Oppbevar minst ett kontrollert medium utenfor boligen.

## Godkjenningskriterier

Piloten er godkjent når:

- alle forventede snapshots kan listes og fullkontrolleres
- eldre snapshots er uendret etter senere kjøringer
- hel restore består `doctor --deep` og uavhengig SHA-256-sammenligning
- enkeltfiler kan hentes uten overskriving
- avbrudd aldri skader et publisert snapshot eller sletter testbilder
- alle avvik, rester og låser gir forståelige meldinger
- utfallet er dokumentert med kommando, exitkode og relevant utskrift

Eventuelle avvik skal registreres i dette dokumentet eller i en egen
pilotrapport før planen markeres som ferdig.


Hele piloten er bestått 24.07.2026 klokken 11:48.

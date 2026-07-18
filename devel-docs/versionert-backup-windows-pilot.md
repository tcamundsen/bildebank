# Windows-pilot for versjonert backup

Denne sjekklisten skal gjennomføres på Windows 11 før første versjon regnes som
ferdig. Bruk en liten testsamling og et eksternt medium av samme type som skal
brukes til virkelig backup. Ingen test skal kjøres mot eneste kopi av bilder.

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

Beregn og lagre en uavhengig liste med SHA-256 og størrelse for mediefilene før
testen. Listen er bare testfasit og skal ligge utenfor repositoryet.

## Oppretting og inkrementell backup

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

Utfør bare disse gjenværende manuelle kontrollene, og bare på testdata:

1. Avbryt én reell snapshotkjøring med Ctrl+C under kopiering på Windows.
   Legg først til en ny testfil med innhold som ikke allerede finnes i
   repositoryet, slik at kjøringen faktisk må kopiere et nytt objekt.
   Kontroller at tidligere publiserte snapshots fortsatt består full kontroll,
   at ingen ny snapshotmappe ble publisert, og at eventuell `incomplete\` blir
   rapportert og bevart. En kontrollert Ctrl+C skal normalt frigjøre låsene.
2. Kontroller resultatet for Windows-junction manuelt bare hvis pytest-testen
   ble hoppet over fordi junction ikke kunne opprettes. Snapshot skal avvise før
   repositorieskriving og ikke følge junctionen.
3. Hvis FAT32 faktisk skal brukes som backupmedium, kjør dry-run mot det fysiske
   mediet med en eksisterende testfil over FAT32-grensen. Filen skal avvises før
   repositorieskriving. Ikke opprett en stor fil og ikke fyll et medium bare
   for denne testen.

Ikke fremprovoser et virkelig fullt vanlig medium. Pytest simulerer `ENOSPC` og
kontrollerer at ingen ny snapshotmappe publiseres, at tidligere snapshots er
uskadet, at låser frigjøres og at `incomplete\` bevares.

## Hel restore

1. Kjør hel restore med `--dry-run` til en manglende mappe.
2. Kontroller at dry-run ikke oppretter mål eller staging.
3. Kjør reell restore og bruk den eksakte tekstbekreftelsen.
4. Kjør `doctor --deep` mot restorekopien.
5. Sammenlign alle mediefiler mot den uavhengige SHA-256-listen.
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
5. Lag et kontrollert `degraded` snapshot og hent både `expected` og `observed`.
6. Kontroller hash-suffikset på `observed`.
7. Lag en kontrollert `recovery_only`-post, finn `entry_id` med
   `snapshot problems` og eksporter posten med `--entry-id`.
8. Avbryt under kopiering og kontroller at en eventuell ufullstendig fil
   bevares og ikke overskrives ved nytt forsøk.

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

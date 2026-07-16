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

## Avbrudd og sikkerhetsgrenser

Utfør bare disse testene på testdata:

1. Avbryt en snapshotkjøring under kopiering.
2. Kontroller at tidligere publiserte snapshots fortsatt består full kontroll.
3. Kontroller at `incomplete\` og eventuell repositorylås rapporteres og ikke
   slettes automatisk.
4. Etter at ingen Bildebank-prosess kjører, håndter låsen etter programmets
   dokumenterte beskjed og bevar incomplete-innholdet for inspeksjon.
5. Forsøk å bruke en ikke-tom vanlig mappe som nytt repository; innholdet skal
   være uendret.
6. Forsøk mål gjennom en symbolsk lenke eller junction; kommandoen skal avvise
   før repositorieskriving.
7. Fyll mediet eller bruk et kontrollert plassestimat som er for stort;
   kommandoen skal avbryte uten publisert snapshot.
8. På FAT32: kontroller at en konkret fil over filsystemets grense avvises før
   skriving. Ikke lag en stor testfil hvis det medfører risiko for andre data.

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

# Permanent sletting av bilder og bruk av tombstone (slettingsmarkør)

Dette dokumentet beskriver design og konsekvenser ved å innføre permanent sletting (tømming av `deleted/`), samt hvordan konseptet **tombstone** (slettingsmarkør) forhindrer utilsiktet re-import.

Planen bygger på database v20. Global unikhet for `files.sha256` ble innført i
v19, og v20 rydder historiske foreldreløse thumbnails og
videoavspillingskopier. Tombstone- og papirkurvlogikken trenger derfor ikke
håndtere flere `files`-rader med samme SHA-256 eller den historiske
cacheoppryddingen.

---

## 1. Bakgrunn og formål

I utgangspunktet følger Bildebank prinsippet om at bilder aldri slettes permanent (med unntak av `unimport`). Ved sletting med `bildebank remove` flyttes filene til mappen `deleted/` i samlingsroten og markeres som slettet i databasen.

Brukere kan likevel ha et reelt behov for permanent sletting:
* **Privatliv og uønskede bilder:** Filer som uforvarende fulgte med fra et minnekort eller en gammel disk (f.eks. personopplysninger, feiltatte bilder eller private filer).
* **Frigjøring av diskplass:** Feiltatte 4K-videoer eller tusenvis av uskarpe serriebilder tar opp mye plass selv om de ligger under `deleted/`.

---

## 2. Problemstilling: Re-import-fellen

Dersom en bildefil slettes fra disken *og samtidig fjernes 100 % fra databasen*, oppstår en fallgruve:

1. Et bilde fra et minnekort importeres til samlingen.
2. Brukeren sletter bildet (`bildebank remove`) og tømmer papirkurven.
3. Filen slettes fra disk og fjernes helt fra databasen.
4. Senere setter brukeren inn det samme minnekortet på nytt for å hente andre bilder.
5. Bildebank skanner minnekortet, sjekker SHA-256-hashen til bildet mot databasen, finner intet spor, og **importerer bildet på nytt**.

---

## 3. Løsning: Tombstone (Slettingsmarkør)

En **tombstone** er en registrering i databasen som blir stående igjen etter at den fysiske bildefilen er slettet fra disken. Tombstones lagres i en egen tabell, `file_tombstones`, og ikke som rader i `files`. Dermed fortsetter `files` å beskrive mediefiler som faktisk finnes i samlingen, enten som aktive filer eller under `deleted/`.

### Hvordan det fungerer
* **Fil på disk:** Slettes fra `deleted/` for å frigjøre diskplass (0 bytes opptatt).
* **Databaseoppføring:** Etter at den fysiske filen er slettet, erstattes den opprinnelige `files`-raden av en rad i `file_tombstones` med informasjon om at filen er eksplisitt slettet permanent av brukeren. Hvis den fysiske slettingen feiler, beholdes `files`-raden og det opprettes ingen tombstone.

### Hva en tombstone inneholder
En tombstone krever lite plass i databasen. Den lagrer:
* `id`: Stabil ID som brukeren kan bruke for å velge en bestemt tombstone.
* `sha256`: SHA-256-hash av den slettede filen. Verdien er unik i tabellen og brukes til duplikatsjekk.
* `size_bytes`: Opprinnelig filstørrelse, for kontroll og rapportering.
* `original_filename`: Filnavnet som visningsinformasjon.
* `former_target_path`: Filens relative plassering i samlingen før `remove`, for eksempel `2024/07/IMG_1234.jpg`.
* `purged_at`: Tidspunktet da den permanente slettingen ble fullført ved at tombstonen ble opprettet og slutt-transaksjonen committed.

Det at en rad finnes i `file_tombstones`, er selve statusen; tabellen trenger ikke et eget statusflagg. Filnavn og tidligere plassering brukes bare for visning og valg av tombstone. SHA-256 er identiteten som styrer import.

`purged_at` er fullføringstidspunktet, ikke et forsøk på å registrere det
eksakte øyeblikket filsystemet fjernet originalen. Ved vanlig sletting er
disse tidspunktene praktisk talt like. Hvis programmet krasjer etter fysisk
sletting, settes `purged_at` når recovery senere fullfører tombstonen og
slutt-transaksjonen. Tidspunktene i `pending_file_purges` beskriver når
slettingen ble bestilt og forsøkt mens den ventet.

Tombstones er uavhengige av importkilder og har ingen kobling til `file_sources`. De uttrykker en beslutning for hele samlingen: Dette eksakte filinnholdet skal ikke importeres igjen, uansett hvilken kilde det senere blir funnet i.

Når Bildebank senere skanner kilder med samme bilde, oppdager import-motoren at SHA-256-hashen matcher en tombstone. Importen kontrollerer også at filstørrelsen stemmer og hopper deretter over filen i stedet for å re-importere den.

### Databaseinvariant mellom filer og tombstones

En SHA-256 kan finnes i enten `files` eller `file_tombstones`, aldri i begge
tabeller samtidig. Unikhetsbegrensningene i hver enkelt tabell er ikke nok
til å håndheve dette på tvers av tabellene. Databasen skal derfor ha
SQLite-triggere som avviser innsetting og endring av SHA-256 i begge tabeller
dersom verdien allerede finnes i den andre.

Når en purge fullføres, fjernes purge-posten først, deretter slettes
`files`-raden, og til slutt opprettes tombstonen i samme
databasetransaksjon. Denne interne rekkefølgen er nødvendig fordi
purge-posten beskytter `files`-raden mot sletting med en restriktiv
fremmednøkkel. Ingen andre databaseforbindelser kan observere
mellomtilstanden. Hvis tombstonen ikke kan opprettes, rulles hele
transaksjonen tilbake, slik at både purge-posten og `files`-raden kommer
tilbake. Applikasjonen skal også kontrollere invarianten før den skriver og
holde target-låsen, slik at forventede konflikter kan rapporteres forståelig
i stedet for bare å gi en triggerfeil.

`doctor` skal kontrollere at ingen SHA-256 finnes i begge tabeller. Hvis en
slik konflikt likevel oppdages, skal den rapporteres uten automatisk
reparasjon. Bildebank kan ikke avgjøre om den fysiske filen eller tombstonen
uttrykker brukerens riktige intensjon.

### Database v21

Tombstone-funksjonaliteten innføres som databaseversjon 21. Migrering fra
v20 skal være en ren skjemamigrering som oppretter `file_tombstones`,
`pending_file_purges`, nødvendige indekser, restriktive fremmednøkler og
krysstabell-triggerne beskrevet ovenfor. Nye databaser skal opprettes direkte
med det samme v21-skjemaet.

Migreringen skal skje i én databasetransaksjon. Den skal ikke skanne
filsystemet, slette filer, endre eksisterende `files`-rader eller opprette
tombstones og purge-poster fra eksisterende innhold. Filer som allerede
ligger under `deleted/`, forblir vanlige filer i papirkurven. En manglende
fysisk fil forblir et avvik som rapporteres av `doctor`; migreringen skal
aldri tolke fraværet som brukerens ønske om permanent sletting.

---

## 4. Konsekvenser for andre operasjoner

### 4.1. Import (`bildebank import`)
* Skanner kildemapper og beregner SHA-256.
* Hvis SHA-256 treffer en eksisterende `files`-rad $\rightarrow$ registreres som duplikatfunn. En fil under `deleted/` forblir slettet.
* Hvis SHA-256 og størrelse treffer en tombstone $\rightarrow$ registreres i importresultatet som eksplisitt forkastet/slettet, og ignoreres. Det opprettes ingen `file_sources`-kobling til tombstonen.
* Hvis SHA-256 treffer en tombstone, men størrelsen er forskjellig, er det en
  integritetskonflikt og ikke et vanlig tombstone-treff. Kildefilen hoppes
  over uten at `files`, `file_sources` eller tombstonen endres. Importen
  fortsetter med de andre filene, og vellykkede importer rulles ikke tilbake.
  Oppsummeringen skal vise at importen ble delvis gjennomført og telle
  integritetsfeilen separat fra duplikater og tombstone-treff. CLI-kommandoen
  skal returnere feilstatus slik at automatisering oppdager konflikten.

### 4.2. Angre import (`bildebank unimport`)
* `unimport` rydder opp importens vanlige koblinger i `file_sources`.
* Tombstones er selvstendige brukerbeslutninger og fjernes ikke av `unimport`.
* En manglende fysisk fil tolereres bare når den er representert av en ferdig tombstone, ikke når en ordinær `files`-rad mangler på disk.

### 4.3. Snapshots
* Historiske snapshots opprettet *før* permanent sletting vil fortsatt inneholde filen i sitt eget snapshot-arkiv på ekstern lagring.
* Nye snapshots opprettet *etter* permanent sletting vil ta med databasen og tombstonen, men skal ikke forvente at den purgede filen finnes under `deleted/`.
* Permanent sletting gjelder bare den aktive samlingen. Eksisterende
  snapshots og andre sikkerhetskopier skal ikke endres eller renses
  automatisk.
* Gjenoppretting av et snapshot fra før slettingen tilbakefører både filene
  og databasen til den eldre tilstanden. Bildet finnes da igjen uten
  tombstone. Hvis brukeren fortsatt ikke ønsker bildet, må det slettes
  permanent på nytt etter gjenopprettingen.
* Eventuell sletting fra snapshot-lagring må være en separat og eksplisitt
  funksjon. Det er ikke en del av denne planen.

### 4.4. Fjerne en tombstone
* Brukeren skal kunne liste og navigere tombstones ved hjelp av ID, opprinnelig filnavn, tidligere plassering, størrelse og slettetidspunkt.
* Brukeren skal kunne velge en bestemt tombstone ved ID og fjerne den med en eksplisitt handling.
* Fjerning av en tombstone gjenoppretter ikke bildefilen. Det opphever bare sperren, slik at samme filinnhold kan importeres igjen ved en senere import.
* Handlingen skal tydelig advare om at filen kan komme tilbake fra enhver fremtidig kilde som inneholder samme filinnhold.
* Handlingen skal bare være tilgjengelig i skrivbar servermodus og bruke
  CSRF-beskyttet `POST`, eksplisitt bekreftelse og target-lås.
* Bekreftelsen skal bindes til tombstonens `id`, SHA-256, størrelse og
  `purged_at`. Under target-låsen skal serveren kontrollere at den samme
  tombstonen fortsatt finnes og er uendret. Ved avvik fjernes ingenting, og
  brukeren må åpne og bekrefte handlingen på nytt.
* Tombstonen fjernes i én databasetransaksjon, og handlingen registreres i
  `command_log`. Det startes ingen import automatisk.

### 4.5. Trygg fysisk sletting
Permanent sletting skal være logisk atomisk: Etter fullført operasjon skal filen enten fortsatt finnes med sin opprinnelige `files`-rad, eller være fysisk borte og ha en tombstone. Filsystemet og SQLite kan ikke inngå i én felles transaksjon, så permanent sletting får en egen tabell, `pending_file_purges`, som varig journal. Tombstonen skal først opprettes etter at den fysiske filen er slettet:

En purge-post er samtidig det varige beviset på at brukeren eksplisitt har
bekreftet permanent sletting av akkurat denne filen. Den skal bare opprettes
som del av en bekreftet permanent slettehandling, aldri fordi en fil mangler,
fordi `doctor` finner et avvik, eller fordi en bakgrunnssjekk antar hva
brukeren ønsket.

1. Etter brukerens bekreftelse legges slettingen i `pending_file_purges` i én databasetransaksjon, med `file_id`, forventet samlingssti, SHA-256, størrelse og alle opplysninger som trengs for å opprette tombstonen. Ved tømming av papirkurven opprettes én purge-post for hver bekreftet og validert fil. Den opprinnelige `files`-raden og tilhørende rader beholdes, og det opprettes ingen tombstone ennå.
2. Databasetransaksjonen committes før Bildebank forsøker fysisk sletting.
3. Oppryddingen kontrollerer at filen på stien fortsatt er den forventede filen før den slettes fra `deleted/`.
4. Før originalen slettes, fjernes alle programgenererte avledede filer som entydig kan knyttes til bildet. Det omfatter gjeldende thumbnail under `thumbs/v2`, videoforhåndsvisning under `video-previews/v1` og eldre thumbnails når stien kan avledes entydig. Oppryddingen skal bare behandle de forventede enkeltfilene, aldri mapper rekursivt. En manglende avledet fil er i orden. En eksisterende avledet fil skal være en vanlig fil uten symbolsk lenke, junction eller annet Windows reparse point. Hvis en slik fil ikke kan valideres eller slettes, beholdes originalen og `files`-raden, og det opprettes ingen tombstone.
5. Når de avledede filene er fjernet, slettes den validerte originalen fra `deleted/`.
6. Etter vellykket fysisk sletting lastes all informasjon som trengs fra purge-posten. Deretter fjernes purge-posten, den opprinnelige `files`-raden med tilhørende `file_sources` og andre relaterte rader slettes, og tombstonen opprettes i denne rekkefølgen i én databasetransaksjon. Hvis noe feiler, rulles hele transaksjonen tilbake.
7. Dersom originalen er endret, erstattet, låst eller ikke kan slettes, beholdes `files`-raden, og det opprettes ingen tombstone. Purge-posten blir stående med feilinformasjon og kan prøves igjen senere. Avledede filer som allerede er fjernet, er regenererbare og trenger ikke gjenopprettes.
8. Hvis programmet stopper etter at originalen er slettet, men før den avsluttende databasetransaksjonen er committed, skal purge-posten inneholde nok informasjon til at recovery kan fullføre sletting av eventuelle gjenstående avledede filer, opprettelsen av tombstonen og oppryddingen av `files` på en entydig måte.
9. Recovery og nye oppryddingsforsøk skal aldri slette en original som ikke har forventet størrelse og SHA-256.

Stikontrollen skal omfatte alle eksisterende komponenter fra den åpne
samlingsroten frem til filen, ikke bare selve filnavnet. Ingen underliggende
mappe eller fil kan være en symbolsk lenke, junction eller annet Windows
reparse point, og den fysisk oppløste stien skal fortsatt ligge innenfor
samlingsroten. De samme reglene gjelder for forventede thumbnails og
videoforhåndsvisninger. Filens identitet skal kontrolleres på nytt umiddelbart
før fysisk sletting.

Dette er et sikkerhetsrekkverk mot uheldige eller kreative lokale
filsystemoppsett, ikke et forsøk på å beskytte mot en lokal administrator som
aktivt endrer filsystemet samtidig. Target-låsen beskytter mot konkurrerende
Bildebank-operasjoner. Hvis en sti er omdirigert, endres underveis eller ikke
kan valideres entydig, skal Bildebank stoppe, beholde originalen og rapportere
årsaken i stedet for å gjette.

Etter at de forventede filene er slettet, kan Bildebank som best-effort
fjerne tomme overordnede mapper under `deleted/`, `thumbs/v2` og
`video-previews/v1`. Oppryddingen skal bare bruke en operasjon som lykkes
dersom mappen faktisk er tom, og den skal stoppe ved den administrerte
rotmappen. Den skal aldri slette mapper rekursivt. Mapper med ukjente filer,
undermapper, lenker eller reparse points skal beholdes. Hvis en tom mappe
ikke kan fjernes, kan det rapporteres som en advarsel, men den permanente
filslettingen og opprettelsen av tombstonen regnes fortsatt som vellykket.

En permanent sletting er ikke fullført før originalen og alle entydig identifiserte avledede filer er borte, tombstonen er opprettet og purge-posten er fjernet. Hvis slettingen feiler, blir den opprinnelige slettede `files`-raden og purge-posten stående, og filen vises som ventende på permanent sletting.

`pending_file_purges` skal minst inneholde:

* entydig ID og `file_id` som er `NOT NULL` og `UNIQUE`
* forventet samlingssti, SHA-256 og størrelse
* opprinnelig filnavn og tidligere samlingssti som skal overføres til tombstonen
* tidspunkt for opprettelse og siste oppdatering
* antall slettingsforsøk og siste feil

`file_id` skal være en fremmednøkkel til `files(id)` med `ON DELETE
RESTRICT` eller tilsvarende `NO ACTION`, aldri `ON DELETE CASCADE`. Dermed
kan ingen vanlig operasjon slette `files`-raden og samtidig miste
purge-journalen. Ved vellykket fullføring fjernes purge-posten først inne i
slutt-transaksjonen som beskrevet ovenfor. Ved avbryting fjernes bare
purge-posten; `files`-raden og den fysiske filen under `deleted/` beholdes.

`pending_file_deletes` beholder sin eksisterende kontrakt som fysisk
etterarbeid for `unimport`, der `files`-raden allerede er fjernet. Køen kan
inneholde både originalen og entydig identifiserte, programgenererte
thumbnails og videoavspillingskopier. De to journaltabellene skal ikke dele
livsløpslogikk. De kan gjenbruke en felles lavnivåfunksjon som validerer sti,
vanlig fil, lenker/reparse points, størrelse og SHA-256 og deretter utfører den
fysiske slettingen.

Target-låsen skal holdes fra før den første valideringen og opprettelsen av purge-posten til den avsluttende databasetransaksjonen er committed, eller et mislykket slettingsforsøk er registrert. Dermed kan ikke snapshot, import, serverhandlinger eller andre samlingsendringer observere eller bygge videre på mellomtilstanden mens prosessen kjører.

Et avbrudd eller strømbrudd kan likevel etterlate en journalført
mellomtilstand. Ved åpning av samlingen skal recovery kontrollere eksisterende
purge-poster under target-låsen:

* Recovery skal bare behandle eksisterende purge-poster. Den kan fullføre den
  permanente slettingen som allerede er godkjent av brukeren, men skal ikke
  opprette nye purge-poster eller utvide slettingen til andre originalfiler.
* Hvis originalen fortsatt finnes og matcher forventet størrelse og SHA-256,
  skal recovery ikke prøve den fysiske slettingen automatisk. Purge-posten
  blir stående til brukeren uttrykkelig velger **Prøv igjen** eller
  **Avbryt permanent sletting**.
* Hvis originalen er borte, fullfører recovery automatisk først sletting av
  eventuelle gjenstående, entydig identifiserte avledede filer og deretter
  opprettelsen av tombstonen og fjerningen av `files`-raden og purge-posten.
  Dette fullfører den tidligere godkjente og allerede påbegynte slettingen.
* Hvis noe annet finnes på stien, skal recovery ikke slette filen eller
  opprette tombstone. Purge-posten beholdes og avviket rapporteres for
  manuell avklaring.
* Hvis samlingen åpnes skrivebeskyttet og recovery derfor ikke kan fullføres, skal mellomtilstanden rapporteres tydelig. Den skal ikke behandles som en ordinær manglende fil eller en ferdig tombstone.

For permanente slettinger som Bildebank har startet etter eksplisitt
bekreftelse, er den varige invarianten at utfallet skal være enten den
opprinnelige filen med sin `files`-rad, eller en ferdig tombstone. En
journalført, ufullført purge beskriver mellomtilstanden og sperrer vanlige
endringer av den aktuelle filen, men skal ikke sperre normal bruk av resten
av samlingen. Purge-posten skal aldri fjernes uten at utfallet er ett av de
to ferdige alternativene, med mindre brukeren avbryter purgen etter reglene
nedenfor.

En fil som mangler uten en tilhørende purge-post, er derimot et ordinært
avvik. Den skal rapporteres av `doctor`; Bildebank skal ikke tolke fraværet
som et ønske om permanent sletting og skal verken opprette purge-post eller
tombstone automatisk. En tombstone skal alltid kunne føres tilbake til en
eksplisitt permanent slettehandling fra brukeren.

#### Avbryte en ventende permanent sletting

Så lenge en `pending_file_purges`-post finnes, eier purge-prosessen
livsløpet til den aktuelle `files`-raden og dens relaterte data. Vanlige
operasjoner som `undelete`, `remove` og `unimport` skal ikke endre filen eller
databaseradene. En ny import som finner samme SHA-256, skal rapportere at
filen venter på permanent sletting og skal ikke legge til en ny
`file_sources`-kobling. Andre muterende operasjoner som treffer raden, skal
avvise eller hoppe over den med en tydelig forklaring.

Brukeren skal kunne velge **Avbryt permanent sletting** dersom den fysiske
originalen fortsatt finnes under forventet sti i `deleted/` og matcher
forventet størrelse og SHA-256. Avbrytingen skal holde target-låsen mens
filen valideres og purge-posten fjernes i en databasetransaksjon. `files`-raden
og filens slettestatus endres ikke av avbrytingen. Etterpå kan brukeren velge
vanlig **Undelete** som en separat handling. Avledede filer kan allerede være
fjernet av et mislykket purge-forsøk; dette hindrer ikke avbryting, siden de
kan genereres på nytt.

Hvis originalen er borte, skal avbryting og `undelete` nektes. Recovery må da
fullføre den tidligere autoriserte permanente slettingen og opprette
tombstone. Hvis noe annet finnes på stien, skal verken avbryting, ny sletting
eller tombstone utføres automatisk; tilstanden må rapporteres for manuell
avklaring.

### 4.6. Papirkurven i Web-UI
For en vanlig bruker er `deleted/` Bildebanks papirkurv. Siden `/settings/removed` skal støtte både permanent sletting av én fil og tømming av hele papirkurven:

* Ved siden av **Undelete** for hver fil skal det være en knapp **Slett permanent**. Handlingen fjerner bildefilen, `files`-raden og relaterte rader, og oppretter en tombstone.
* Siden skal ha en knapp **Tøm papirkurven**. Den behandler alle databaseførte filer som tilfredsstiller kriteriene nedenfor.
* Begge handlingene skal vise hva som skal slettes og kreve eksplisitt bekreftelse før database eller filer endres.
* Muterende kall skal bare være tilgjengelige i skrivbar servermodus, bruke CSRF-beskyttet `POST` og holde target-låsen gjennom hele den logisk atomiske operasjonen beskrevet ovenfor.

Target-låsen kan ikke holdes mens brukeren leser og svarer på
bekreftelsesdialogen. Forhåndsvisningen for **Tøm papirkurven** skal derfor
bindes til det eksakte settet med filer som ble vist. For hver fil skal
bekreftelsesgrunnlaget inneholde `file_id`, SHA-256, størrelse, forventet
relativ sti under `deleted/` og `deleted_at`. Når brukeren bekrefter, skal
serveren ta target-låsen og kreve at hele denne identiteten fortsatt stemmer
med databasen og den fysiske filen. Den skal ikke skanne papirkurven på nytt
og utvide utvalget med filer som har kommet til etter forhåndsvisningen.
Filer som ikke lenger finnes, har blitt endret, har fått gjenbrukt
`file_id`, eller ikke lenger tilfredsstiller kriteriene, skal hoppes over og
rapporteres. Nye filer blir liggende til en senere, separat bekreftelse.
Tilsvarende skal bekreftelsen for **Slett permanent** være bundet til hele
identiteten til den konkrete filen som ble vist.

Denne kontrollen beskytter i dagens system mot gamle sider, flere
nettleserfaner, samtidige CLI-kommandoer og flere Bildebank-prosesser mot
samme samling. Den gjør også bekreftelsesmodellen egnet dersom systemet senere
bygges ut for flere samtidige brukere.

Ved permanent sletting av én fil er brukerflyten:

1. Brukeren klikker **Slett permanent** og får et bekreftelsesspørsmål.
2. Hvis brukeren svarer ja, opprettes purge-posten og den fysiske slettingen forsøkes. Tombstonen opprettes først hvis filen blir slettet.
3. Hvis den fysiske slettingen feiler, viser dialogen **Sletting feilet. Prøv igjen? JA/NEI** sammen med feilårsaken.
4. **JA** prøver den eksisterende purge-posten på nytt med samme forventede sti, størrelse og SHA-256. Det opprettes ikke en ny tombstone eller purge-post.
5. **NEI** avslutter dialogen. Den opprinnelige `files`-raden, purge-posten og den fysiske filen blir stående uten tombstone, og `/settings/removed` viser filen som **Venter på permanent sletting** med mulighet for et senere nytt forsøk.

For en fil som vises som **Venter på permanent sletting**, erstattes de
vanlige knappene **Undelete** og **Slett permanent** av **Prøv igjen** og
**Avbryt permanent sletting**. **Prøv igjen** behandler den eksisterende
purge-posten. **Avbryt permanent sletting** følger validerings- og
avbrytingsreglene ovenfor; først etter vellykket avbryting blir vanlig
**Undelete** tilgjengelig igjen.

I det sjeldne tilfellet der originalen allerede er borte, men recovery ikke
klarer å fullføre for eksempel på grunn av en låst avledet fil, skal UI vise
den enkle statusen **Venter på å fullføre permanent sletting** med **Prøv
igjen**. **Undelete** og **Avbryt permanent sletting** skal da ikke tilbys.
Brukeren trenger ikke få presentert den interne forskjellen mellom fysisk
original, avledede filer og slutt-transaksjonen. Hvis recovery lykkes ved
neste åpning, fullføres dette uten en egen brukerhandling eller melding.

Ved **Tøm papirkurven** skal feil for én fil ikke hindre permanent sletting av andre validerte filer. Bildebank forsøker alle filene i det bekreftede utvalget. Filer som kan slettes, fjernes og får tombstone som planlagt. For filer som ikke kan slettes, beholdes `files`-raden og den fysiske filen uten tombstone, mens purge-posten blir stående som ventende på permanent sletting.

Hvis én eller flere fysiske filer ikke kunne slettes, viser Web-UI dialogen **Enkelte filer kunne ikke slettes. [Lukk]** etter at alle filene er forsøkt. Dialogen har bare knappen **Lukk** og tilbyr ikke et samlet nytt forsøk. Etter at dialogen lukkes, viser `/settings/removed` de aktuelle filene som **Venter på permanent sletting**.

Filer som allerede har en `pending_file_purges`-post, skal tas med ved en
senere forhåndsvisning av **Tøm papirkurven** og merkes tydelig som **Nytt
forsøk**. Den nye bekreftelsen gir tillatelse til å prøve den eksisterende
purgen igjen. Bildebank skal gjenbruke purge-posten og aldri forsøke å
opprette en ny post for samme `file_id`.

Før et nytt forsøk må identiteten i purge-posten,
bekreftelsesøyeblikksbildet og `files`-raden stemme. Hvis originalen fortsatt
finnes og matcher purge-posten, prøves hele slettingen på nytt. Hvis
originalen er borte, prøves bare gjenstående avledet opprydding og
slutt-transaksjonen som oppretter tombstonen. Hvis noe annet finnes på
stien, utføres ingen sletting. Ved nytt avvik beholdes den samme purge-posten
med oppdatert forsøksteller og siste feil. Avbryting av permanent sletting
gjøres fortsatt per fil og er ikke en del av **Tøm papirkurven**.

En ny permanent sletting kan bare startes når:

1. Den har en `files`-rad med `deleted_at`.
2. Den databaseførte samlingsstien er gyldig og ligger under `deleted/`, og
   alle eksisterende stikomponenter under samlingsroten består
   sikkerhetskontrollen ovenfor.
3. Filen finnes som en vanlig fil uten symbolsk lenke, junction eller annet Windows reparse point.
4. Filens størrelse og SHA-256 stemmer med `files`-raden.

For filer uten purge-post skal **Tøm papirkurven** bare starte permanent
sletting når alle fire kontrollene består. En eksisterende, bekreftet
purge-post følger derimot retry-reglene ovenfor og kan fullføres når
originalen allerede er borte. Resultatet skal skille mellom filer som ble
slettet, filer som venter på et nytt slettingsforsøk, og filer som ble hoppet
over ved validering. Manglende, endrede eller ugyldige databaseførte filer
uten purge-post skal bli stående urørt og vises i resultatet med årsak. Filer
under `deleted/` som ikke har en tilhørende `files`-rad, er ukjente filer og
skal aldri slettes eller få tombstone av denne funksjonen.

---

## 5. Retningslinjer for sikkerhet og brukerflate

Dersom permanent sletting / tømming av papirkurv implementeres, må følgende sikkerhetsprinsipper overholdes:

1. **Forhåndsvisning og bekreftelse:**
   * Web-UI skal vise antall filer og samlet størrelse før brukeren bekrefter permanent sletting.
   * Dialogen skal tydelig opplyse om at permanent sletting bare gjelder den
     aktive samlingen, og at eldre snapshots og andre sikkerhetskopier
     fortsatt kan inneholde filene.
   * Bekreftelsen skal være bundet til identitetsøyeblikksbildet for hver fil
     som ble forhåndsvist: `file_id`, SHA-256, størrelse, forventet relativ
     sti under `deleted/` og `deleted_at`. Serveren skal validere dette
     eksakte utvalget på nytt under target-låsen og skal aldri legge til nye
     kandidater automatisk.
   * En eventuell CLI-kommando skal være dry-run som standard og kreve et eksplisitt bekreftelsesflagg for å utføre sletting.
2. **Avgrenset utvalg:** Bare validerte, databaseførte filer under `deleted/` kan behandles. Funksjonen skal ikke implementeres som rekursiv sletting av innholdet i mappen.
   * Programgenererte thumbnails og videoforhåndsvisninger kan bare slettes når den forventede enkeltstien kan avledes entydig fra purge-posten. Andre filer i de avledede mappene skal ikke berøres.
3. **Eksplisitt fjerning av tombstone:**
   * Dersom brukeren vil at Bildebank også skal glemme sperren, skal en bestemt tombstone kunne fjernes ved ID.
   * Handlingen skal vise tombstonens filnavn, tidligere plassering, størrelse og slettetidspunkt før bekreftelse, og advare om at re-import da kan skje ved fremtidig skanning.
   * Bekreftelsen skal være bundet til tombstonens `id`, SHA-256, størrelse og
     `purged_at`. Fjerningen skal bruke target-lås og en
     databasetransaksjon, og skal avvises dersom tombstonen er endret.
4. **Logging og historikk:**
   * `command_log` skal registrere selve handlingen, om den gjaldt én fil eller tømming av papirkurven, og antall kandidater, slettede filer, ventende slettinger og filer som ble hoppet over.
   * `file_tombstones` er historikken per permanent slettet fil. Fillister, filnavn og SHA-256 skal ikke dupliseres i `command_log.args_json`.
   * `pending_file_purges` inneholder filspesifikke opplysninger og feil for permanente slettinger som ikke er fullført. `pending_file_deletes` fortsetter å beskrive fysisk etterarbeid for `unimport`.

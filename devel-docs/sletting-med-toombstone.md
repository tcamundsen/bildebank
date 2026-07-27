# Permanent sletting av bilder og bruk av tombstone (slettingsmarkør)

Dette dokumentet beskriver design og konsekvenser ved å innføre permanent sletting (tømming av `deleted/`), samt hvordan konseptet **tombstone** (slettingsmarkør) forhindrer utilsiktet re-import.

Planen bygger på database v19, der `files.sha256` allerede er globalt unik og
eventuelle eldre duplikatrader repareres under migrering. Tombstone- og
papirkurvlogikken trenger derfor ikke håndtere flere `files`-rader med samme
SHA-256.

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
* `purged_at`: Tidspunktet da bildefilen ble permanent slettet.

Det at en rad finnes i `file_tombstones`, er selve statusen; tabellen trenger ikke et eget statusflagg. Filnavn og tidligere plassering brukes bare for visning og valg av tombstone. SHA-256 er identiteten som styrer import.

Tombstones er uavhengige av importkilder og har ingen kobling til `file_sources`. De uttrykker en beslutning for hele samlingen: Dette eksakte filinnholdet skal ikke importeres igjen, uansett hvilken kilde det senere blir funnet i.

Når Bildebank senere skanner kilder med samme bilde, oppdager import-motoren at SHA-256-hashen matcher en tombstone. Importen kontrollerer også at filstørrelsen stemmer og hopper deretter over filen i stedet for å re-importere den.

---

## 4. Konsekvenser for andre operasjoner

### 4.1. Import (`bildebank import`)
* Skanner kildemapper og beregner SHA-256.
* Hvis SHA-256 treffer en eksisterende `files`-rad $\rightarrow$ registreres som duplikatfunn. En fil under `deleted/` forblir slettet.
* Hvis SHA-256 treffer en tombstone $\rightarrow$ registreres i importresultatet som eksplisitt forkastet/slettet, og ignoreres. Det opprettes ingen `file_sources`-kobling til tombstonen.

### 4.2. Angre import (`bildebank unimport`)
* `unimport` rydder opp importens vanlige koblinger i `file_sources`.
* Tombstones er selvstendige brukerbeslutninger og fjernes ikke av `unimport`.
* En manglende fysisk fil tolereres bare når den er representert av en ferdig tombstone, ikke når en ordinær `files`-rad mangler på disk.

### 4.3. Snapshots
* Historiske snapshots opprettet *før* permanent sletting vil fortsatt inneholde filen i sitt eget snapshot-arkiv på ekstern lagring.
* Nye snapshots opprettet *etter* permanent sletting vil ta med databasen og tombstonen, men skal ikke forvente at den purgede filen finnes under `deleted/`.

### 4.4. Fjerne en tombstone
* Brukeren skal kunne liste og navigere tombstones ved hjelp av ID, opprinnelig filnavn, tidligere plassering, størrelse og slettetidspunkt.
* Brukeren skal kunne velge en bestemt tombstone ved ID og fjerne den med en eksplisitt handling.
* Fjerning av en tombstone gjenoppretter ikke bildefilen. Det opphever bare sperren, slik at samme filinnhold kan importeres igjen ved en senere import.
* Handlingen skal tydelig advare om at filen kan komme tilbake fra enhver fremtidig kilde som inneholder samme filinnhold.

### 4.5. Trygg fysisk sletting
Permanent sletting skal være logisk atomisk: Etter fullført operasjon skal filen enten fortsatt finnes med sin opprinnelige `files`-rad, eller være fysisk borte og ha en tombstone. Filsystemet og SQLite kan ikke inngå i én felles transaksjon, så permanent sletting får en egen tabell, `pending_file_purges`, som varig journal. Tombstonen skal først opprettes etter at den fysiske filen er slettet:

1. I én databasetransaksjon legges slettingen i `pending_file_purges` med `file_id`, forventet samlingssti, SHA-256, størrelse og alle opplysninger som trengs for å opprette tombstonen. Den opprinnelige `files`-raden og tilhørende rader beholdes, og det opprettes ingen tombstone ennå.
2. Databasetransaksjonen committes før Bildebank forsøker fysisk sletting.
3. Oppryddingen kontrollerer at filen på stien fortsatt er den forventede filen før den slettes fra `deleted/`.
4. Etter vellykket fysisk sletting opprettes tombstonen, den opprinnelige `files`-raden med tilhørende `file_sources` og andre relaterte rader fjernes, og purge-posten slettes i én databasetransaksjon.
5. Dersom filen er endret, erstattet, låst eller ikke kan slettes, beholdes `files`-raden, og det opprettes ingen tombstone. Purge-posten blir stående med feilinformasjon og kan prøves igjen senere.
6. Hvis programmet stopper etter at filen er slettet, men før den avsluttende databasetransaksjonen er committed, skal purge-posten inneholde nok informasjon til at recovery kan fullføre opprettelsen av tombstonen og oppryddingen av `files` på en entydig måte.
7. Recovery og nye oppryddingsforsøk skal aldri slette en fil som ikke har forventet størrelse og SHA-256.

En permanent sletting er ikke fullført før den fysiske filen er borte, tombstonen er opprettet og purge-posten er fjernet. Hvis slettingen feiler, blir den opprinnelige slettede `files`-raden og purge-posten stående, og filen vises som ventende på permanent sletting.

`pending_file_purges` skal minst inneholde:

* entydig ID og unik `file_id`
* forventet samlingssti, SHA-256 og størrelse
* opprinnelig filnavn og tidligere samlingssti som skal overføres til tombstonen
* tidspunkt for opprettelse og siste oppdatering
* antall slettingsforsøk og siste feil

`pending_file_deletes` beholder sin eksisterende kontrakt som fysisk etterarbeid for `unimport`, der `files`-raden allerede er fjernet. De to tabellene skal ikke dele livsløpslogikk. De kan gjenbruke en felles lavnivåfunksjon som validerer sti, vanlig fil, lenker/reparse points, størrelse og SHA-256 og deretter utfører den fysiske slettingen.

Target-låsen skal holdes fra før den første valideringen og opprettelsen av purge-posten til den avsluttende databasetransaksjonen er committed, eller et mislykket slettingsforsøk er registrert. Dermed kan ikke snapshot, import, serverhandlinger eller andre samlingsendringer observere eller bygge videre på mellomtilstanden mens prosessen kjører.

Et avbrudd eller strømbrudd kan likevel etterlate en journalført mellomtilstand. Før samlingen brukes normalt igjen, skal recovery behandle den slik:

* Hvis filen fortsatt finnes og matcher forventet størrelse og SHA-256, kan det autoriserte slettingsforsøket prøves igjen.
* Hvis filen er borte, fullfører recovery opprettelsen av tombstonen og fjerningen av `files`-raden og purge-posten.
* Hvis noe annet finnes på stien, skal recovery ikke slette filen eller opprette tombstone, men stoppe for manuell avklaring.
* Hvis samlingen åpnes skrivebeskyttet og recovery derfor ikke kan fullføres, skal mellomtilstanden rapporteres tydelig. Den skal ikke behandles som en ordinær manglende fil eller en ferdig tombstone.

Den varige invarianten er at en fysisk manglende fil alltid skal ha enten en ferdig tombstone eller en journalført, ufullført purge som må fullføres før normal bruk. Purge-posten skal aldri fjernes uten at utfallet er ett av de to ferdige alternativene.

### 4.6. Papirkurven i Web-UI
For en vanlig bruker er `deleted/` Bildebanks papirkurv. Siden `/settings/removed` skal støtte både permanent sletting av én fil og tømming av hele papirkurven:

* Ved siden av **Undelete** for hver fil skal det være en knapp **Slett permanent**. Handlingen fjerner bildefilen, `files`-raden og relaterte rader, og oppretter en tombstone.
* Siden skal ha en knapp **Tøm papirkurven**. Den behandler alle databaseførte filer som tilfredsstiller kriteriene nedenfor.
* Begge handlingene skal vise hva som skal slettes og kreve eksplisitt bekreftelse før database eller filer endres.
* Muterende kall skal bare være tilgjengelige i skrivbar servermodus, bruke CSRF-beskyttet `POST` og holde target-låsen gjennom hele den logisk atomiske operasjonen beskrevet ovenfor.

Ved permanent sletting av én fil er brukerflyten:

1. Brukeren klikker **Slett permanent** og får et bekreftelsesspørsmål.
2. Hvis brukeren svarer ja, opprettes purge-posten og den fysiske slettingen forsøkes. Tombstonen opprettes først hvis filen blir slettet.
3. Hvis den fysiske slettingen feiler, viser dialogen **Sletting feilet. Prøv igjen? JA/NEI** sammen med feilårsaken.
4. **JA** prøver den eksisterende purge-posten på nytt med samme forventede sti, størrelse og SHA-256. Det opprettes ikke en ny tombstone eller purge-post.
5. **NEI** avslutter dialogen. Den opprinnelige `files`-raden, purge-posten og den fysiske filen blir stående uten tombstone, og `/settings/removed` viser filen som **Venter på permanent sletting** med mulighet for et senere nytt forsøk.

Ved **Tøm papirkurven** skal feil for én fil ikke hindre permanent sletting av andre validerte filer. Bildebank forsøker alle filene i det bekreftede utvalget. Filer som kan slettes, fjernes og får tombstone som planlagt. For filer som ikke kan slettes, beholdes `files`-raden og den fysiske filen uten tombstone, mens purge-posten blir stående som ventende på permanent sletting.

Hvis én eller flere fysiske filer ikke kunne slettes, viser Web-UI dialogen **Enkelte filer kunne ikke slettes. [Lukk]** etter at alle filene er forsøkt. Dialogen har bare knappen **Lukk** og tilbyr ikke et samlet nytt forsøk. Etter at dialogen lukkes, viser `/settings/removed` de aktuelle filene som **Venter på permanent sletting**.

En fil kan bare slettes permanent og få tombstone når:

1. Den har en `files`-rad med `deleted_at`.
2. Den databaseførte samlingsstien er gyldig og ligger under `deleted/`.
3. Filen finnes som en vanlig fil uten symbolsk lenke, junction eller annet Windows reparse point.
4. Filens størrelse og SHA-256 stemmer med `files`-raden.

**Tøm papirkurven** skal bare behandle filer som består alle fire kontrollene. Resultatet skal skille mellom filer som ble slettet, filer som venter på et nytt slettingsforsøk, og filer som ble hoppet over ved validering. Manglende, endrede eller ugyldige databaseførte filer skal bli stående urørt og vises i resultatet med årsak. Filer under `deleted/` som ikke har en tilhørende `files`-rad, er ukjente filer og skal aldri slettes eller få tombstone av denne funksjonen.

---

## 5. Retningslinjer for sikkerhet og brukerflate

Dersom permanent sletting / tømming av papirkurv implementeres, må følgende sikkerhetsprinsipper overholdes:

1. **Forhåndsvisning og bekreftelse:**
   * Web-UI skal vise antall filer og samlet størrelse før brukeren bekrefter permanent sletting.
   * En eventuell CLI-kommando skal være dry-run som standard og kreve et eksplisitt bekreftelsesflagg for å utføre sletting.
2. **Avgrenset utvalg:** Bare validerte, databaseførte filer under `deleted/` kan behandles. Funksjonen skal ikke implementeres som rekursiv sletting av innholdet i mappen.
3. **Eksplisitt fjerning av tombstone:**
   * Dersom brukeren vil at Bildebank også skal glemme sperren, skal en bestemt tombstone kunne fjernes ved ID.
   * Handlingen skal vise tombstonens filnavn, tidligere plassering, størrelse og slettetidspunkt før bekreftelse, og advare om at re-import da kan skje ved fremtidig skanning.
4. **Logging og historikk:**
   * `command_log` skal registrere selve handlingen, om den gjaldt én fil eller tømming av papirkurven, og antall kandidater, slettede filer, ventende slettinger og filer som ble hoppet over.
   * `file_tombstones` er historikken per permanent slettet fil. Fillister, filnavn og SHA-256 skal ikke dupliseres i `command_log.args_json`.
   * `pending_file_purges` inneholder filspesifikke opplysninger og feil for permanente slettinger som ikke er fullført. `pending_file_deletes` fortsetter å beskrive fysisk etterarbeid for `unimport`.

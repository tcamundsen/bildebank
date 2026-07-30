# Tilfeldig bilde og registrering av bildevisninger

## Mål

Bildebank skal kunne hjelpe brukeren å oppdage bilder som ellers lett blir
glemt. Det krever at samlingen vet om et bilde faktisk har vært sett, og ikke
bare at det har vært med i et rutenett eller at nettleseren har hentet en
thumbnail.

Funksjonen består av to deler:

- registrere en kvalifisert visning av et bilde
- åpne et tilfeldig usett bilde, eller når alle aktive bilder er sett, et
  tilfeldig bilde som ikke har vært sett på lenge

Statistikken er samlingsinformasjon, ikke en brukerkonto- eller
nettleserhistorikk. Den følger derfor bildesamlingen og gjelder alle som bruker
den samme skrivbare samlingen.

## Når et bilde regnes som sett

En visning teller bare når alle disse vilkårene er oppfylt:

1. Bildet eller videoen vises på en full enkeltbildeside i den felles
   browserflyten, for eksempel `/item/<id>`,
   `/person/<navn>/item/<id>`, et importutvalg eller et filterutvalg.
2. For et stillbilde har siden vært synlig i nettleserfanen sammenhengende i
   minst to sekunder etter at hovedbildet er ferdig lastet nok til å kunne
   vises.
3. For en video er minst åtte sekunder av selve videoen spilt av mens fanen er
   synlig. Avspillingstid før eller etter et hopp i tidslinjen teller ikke.
   En video som er kortere enn åtte sekunder teller når den er spilt helt til
   slutt.
4. Siden er fortsatt den samme enkeltbildesiden når vilkåret er oppfylt.

Bildetimeren nullstilles når fanen blir skjult, brukeren forlater siden, eller
hovedmediet ikke kan lastes. Videotelleren går bare mens videoen faktisk spiller
og fanen er synlig; pause og skjult fane teller ikke. En visning registreres
høyst én gang per innlasting av enkeltbildesiden. Tastaturnavigasjon og
navigasjonsknapper gir ny sideinnlasting og kan derfor gi en ny visning når den
nye siden oppfyller kravet.

Måneds-, års-, søke- og andre rutenettvisninger teller aldri, heller ikke når
en thumbnail er synlig lenge. Forhåndslasting, direkte medie-URL-er og
metadata- eller thumbnail-forespørsler teller heller ikke.

`--slideshow` omfattes ikke i første versjon. Slideshowet er en separat,
streng read-only-rute uten POST-endepunkter. Det kan få en egen, uttrykkelig
design senere dersom visninger der også skal lagres.

## Datamodell og livssyklus

Visningsstatistikken skal ligge i en egen tabell, ikke som kolonner på
`files`:

```sql
CREATE TABLE file_view_stats (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    view_count INTEGER NOT NULL CHECK (view_count >= 1),
    first_viewed_at TEXT NOT NULL,
    last_viewed_at TEXT NOT NULL
);

CREATE INDEX idx_file_view_stats_last_viewed_at
ON file_view_stats(last_viewed_at, file_id);
```

En manglende rad betyr at filen aldri er registrert sett. Tidspunktene settes av
serveren og lagres som UTC i det samme tekstformatet som øvrige tidsstempler i
databasen; nettleserens klokke brukes ikke. Ved en gyldig visning opprettes
raden med `view_count = 1`, eller oppdateres atomisk med
`view_count = view_count + 1` og nytt `last_viewed_at`.

`file_id` peker på den kanoniske `files`-raden. `file_sources` er bare
kildereferanser til samme lagrede fil, så statistikken må ikke ligge på
`file_sources` og må ikke dobles når den samme filen er importert fra flere
kilder. Når `unimport` beholder en `files`-rad fordi den fortsatt har andre
`file_sources`, beholdes statistikken. Fjernes `files`-raden, fjernes
statistikken med `ON DELETE CASCADE`; det samme gjelder ved den bekreftede
purge-flyten. `remove` og `undelete` beholder `files`-raden og dermed
statistikken, men fjernede filer tas ikke med i utvalget.

Endringen er en ordinær hoveddatabase-migrering fra gjeldende schema v21 til
v22. Migreringen oppretter tabellen og indeksen uten å gjette gamle
visningsdata. Nye og eksisterende bilder starter derfor som usette.

## Server og klient

En enkeltbildeside skal merke hovedområdet med fil-ID. For stillbilder starter
`server.js` timeren først når hovedbildet er klart og dokumentet er synlig.
Terskelen velges per nettleserprofil i Innstillinger: 0,5, 1, 2, 3 eller 5
sekunder. Den lagres i nettleserens lokale lagring, slik at ulike brukere ikke
endrer hverandres valg; standard og fallback er 0,5 sekunder. For videoer
følger den videoelementets avspillings- og seek-hendelser, slik at bare reell
avspilling uten hopping framover teller. Når grensen er nådd, sender den
ett CSRF-beskyttet POST-kall til `/api/item-viewed` med fil-ID. Kallet er en
bakgrunnsoppdatering; feil skal ikke endre navigasjonen eller vise en
feilmelding til brukeren. Når serveren bekrefter at registreringen faktisk er
lagret, vises den diskrete, vedvarende markøren `(sett)` etter filnavnet i
brødsmulen. Markøren vises ikke ved tyst hopp over eller feil.

Endepunktet skal:

- bare være tilgjengelig for den normale, skrivbare serveren
- validere CSRF og en positiv heltalls-ID på vanlig måte
- kontrollere i databasen at ID-en fortsatt er en aktiv `files`-rad
- oppdatere statistikken i én kort transaksjon
- svare med `200 OK` og `{"recorded": true}` etter vellykket registrering
- svare med `204 No Content` når oppdateringen tyst hoppes over fordi
  samlingen er opptatt eller filen ikke lenger er aktiv

Read-only-server, LAN-share og slideshow skal ikke skrive statistikk. Klienten
skal da ikke starte timeren eller sende POST-kallet. Dersom en kortvarig
databasekonflikt oppstår mens import, remove, undelete eller purge holder på,
skal visningsregistreringen avbrytes raskt og tyst; den må aldri forsinke
visningen eller konkurrere med en operasjon som endrer samlingen.

## Tilfeldig bilde

`GET /random` velger bare blant aktive bilde- og videorader i `files` som kan
vises i den ordinære browseren, og videresender til `/item/<id>`. Ruten endrer
ikke statistikk selv; det skjer først etter en kvalifisert visning.

Utvalget er:

1. Hvis det finnes usette aktive filer, velges én tilfeldig av disse.
2. Ellers velges én tilfeldig fil fra den eldste poolen etter
   `last_viewed_at`: `min(antall aktive filer, maks(20, tak(5 % av antall
   aktive filer)))` filer.
3. Hvis samlingen ikke har aktive filer, vises en vanlig tomtilstands-/feilside
   i stedet for redirect.

Dette gir først bred oppdagelse av usette bilder, og deretter variasjon blant
bilder som har ligget lengst. Rangeringen bruker `last_viewed_at`, ikke bare
`view_count`: et bilde som er blitt sett mange ganger for lenge siden skal
fortsatt kunne dukke opp igjen.

En lenke eller knapp med teksten «Tilfeldig bilde» skal være tilgjengelig i
den vanlige browsernavigasjonen. Dashboardet skal vise «Registrert sett» som
antall unike, kvalifiserte bilde- og videofiler med en rad i
`file_view_stats`, av det totale antallet kvalifiserte aktive filer, for
eksempel `327 av 14499`. Det skal ikke legges til sortering etter
visningsstatistikk i eksisterende oversikter.

## Implementasjonsdetaljer

### Klientregistrering

`source_item_page_html` skal sende en eksplisitt boolsk data-attributt som
forteller om visningsregistrering er tillatt. Attributtet skal være avslått for
read-only, LAN-share og slideshow; klienten skal ikke forsøke å utlede dette
fra URL eller feilrespons.

For et stillbilde skal klienten vente på `load`-hendelsen på hovedbildet. Hvis
bildet allerede er lastet når koden installerer lytteren, brukes `complete` og
gyldig bildebredde som tilsvarende bekreftelse. Først deretter starter den
valgte timeren. `visibilitychange`, `pagehide` og mislykket bildelasting
avbryter timeren. Det er én klientflaggsvariabel som hindrer mer enn ett POST
kall for samme sideinnlasting.

For video skal klienten måle framdrift i videoens egen tidslinje
(`HTMLMediaElement.currentTime`), ikke i nettleserens veggklokke. Den
summerer bare positive tidsintervaller mens videoen spiller, dokumentet er
synlig og videoen ikke søker. `seeking`, `seeked`, `loadedmetadata` og
`emptied` nullstiller referansepunktet, slik at et hopp framover aldri kan
legge til avspillingstid. Pause, buffering og skjult fane legger ikke til tid;
avspilling etterpå kan fortsette å bygge opp den allerede gyldige summen.
Avspillingshastighet følger videoens tidslinje, slik at åtte sekunder ved 2×
er åtte sekunder med videoinnhold.

Når den akkumulerte gyldige avspillingstiden når åtte sekunder, registreres
visningen. For en video med kjent varighet under åtte sekunder registreres den
bare ved `ended`, og bare når den akkumulerte gyldige tiden dekker hele
varigheten med normal avrundingstoleranse. En seeket video kan derfor ikke
registreres bare ved å hoppe til slutten. Videoer med ukjent eller ugyldig
varighet bruker åttesekundersregelen og får ikke kortvideo-unntaket.

POST-kallet bruker den eksisterende `csrfFetch`-hjelperen og sender en liten
JSON-body med bare `file_id` til `/api/item-viewed`. Det sendes ikke
`source_url`, klienttid eller avspillingsdata; serveren trenger bare å avgjøre
om den aktuelle, aktive filen skal få en ny kvalifisert visning.

### Serverregistrering

Endepunktet skal bruke samme JSON-validering og CSRF-kontroll som de øvrige
item-API-ene. Etter valideringen åpnes en kortvarig skrivbar tilkobling med
ingen eller svært kort SQLite-ventetid. Den skal ikke ta `TargetLock`, siden
den ikke flytter eller endrer bildefiler og ikke må vente på en pågående
operasjon.

I én transaksjon skal endepunktet sette et nytt UTC-tidspunkt på serveren og
utføre en `INSERT ... ON CONFLICT DO UPDATE` som bare virker når `file_id`
fremdeles peker på en `files`-rad med `deleted_at IS NULL`. Første registrering
setter både `first_viewed_at` og `last_viewed_at`; senere registrering øker
`view_count` og endrer bare `last_viewed_at`. Ugyldig, ukjent eller fjernet ID
gir ingen databaseendring.

Hvis samlingens target-lås allerede finnes, eller SQLite svarer `busy` eller
`locked`, skal endepunktet lukke tilkoblingen og svare `204 No Content` uten
endring. Andre databasefeil skal logges på serveren og gi vanlig feilrespons;
klientkoden ignorerer svaret. Dette gjør at en feil ikke påvirker visningen,
men skjuler ikke programmerings- eller databasefeil som bør undersøkes.

### Tilfeldig utvalg

Utvalget skal bruke den eksisterende `media_kind()`-klassifiseringen og bare
ta med aktive rader der resultatet er `image` eller `video`. Dermed kommer
ikke sidecar-filer eller andre filer som kan ha en enkeltbildeside, med ved en
feil. En felles databasehjelper skal eie utplukkingen, slik at HTTP-ruten bare
videresender resultatet til riktig `/item/<id>`.

Hjelperen finner først alle kvalifiserte usette kandidater med en venstrejoin
mot `file_view_stats`. Finnes det ingen, finner den den eldste definerte poolen
etter `last_viewed_at`. Den trekker deretter tilfeldig fra den allerede
avgrensede poolen. Tomt resultat returneres som `None`; ruten skal ikke finne
på en ID eller redirecte til browserroten.

## Tester

- Migrering fra v21 oppretter tabellen og indeksen uten å endre eksisterende
  `files` eller `file_sources`.
- Første og gjentatt registrering oppdaterer teller og tid korrekt; ukjente,
  fjernede og ugyldige fil-ID-er endrer ingenting.
- Registrering ignoreres i read-only, LAN-share og slideshow, og ved en
  kortvarig skrivekonflikt.
- En enkeltbildeside har klientmarkering og klientkode som krever to sekunder
  synlig, lastet stillbilde; rutenett har det ikke.
- Videoer krever åtte sekunder reell, synlig avspilling uten hopping framover.
  Seeking, skjult fane og pause teller ikke. Kortere videoer registreres først
  etter faktisk avspilling helt til slutt.
- Klienten sender bare `file_id`, og serveren bruker sitt eget UTC-tidspunkt.
  Target-lås, SQLite `busy` og `locked` gir tyst `204` uten databaseendring;
  andre databasefeil skjules ikke som en registrert visning.
- Tilfeldig utvalg foretrekker alltid usette aktive filer, utelater fjernede
  filer og bruker bare den eldste utvalgspoolen når ingen er usett.
- Statistikk overlever `remove` og `undelete`, beholdes når `unimport` lar den
  kanoniske filen stå på grunn av en annen `file_sources`-rad, og slettes når
  selve `files`-raden slettes.

## Implementasjonsplan

### 1. Migrer hoveddatabasen til v22

1. Øk `SCHEMA_VERSION` fra 21 til 22 i `bildebank/db_schema.py`.
2. Legg til én idempotent skjemaoppretter for `file_view_stats`, inkludert
   foreign key med `ON DELETE CASCADE` og indeksen for `last_viewed_at`.
   Kall den både når en ny database opprettes og i v21→v22-migreringen.
3. Utvid `MigrationPlan`, migreringsrapportering og
   `validate_current_schema()` slik at manglende tabell, feil kolonner,
   manglende foreign key eller indeks gir den vanlige beskjeden om å kjøre
   `bildebank migrate`.
4. Utvid migreringstestene med en database på v21 som allerede har
   `files` og `file_sources`. Verifiser at migreringen bare oppretter den nye
   strukturen og beholder eksisterende bilder og kildehenvisninger uendret.

### 2. Innfør små databasehjelpere

1. Opprett `bildebank/db_view_stats.py` og re-eksporter de offentlige
   hjelperne fra `bildebank/db.py`. Hold modulen begrenset til
   visningsstatistikk og tilfeldig-utvalg; ikke bland den med import- eller
   filflyttelogikk.
2. Implementer en hjelpefunksjon som, med en åpen skrivbar tilkobling og et
   servergenerert UTC-tidspunkt, atomisk oppretter eller oppdaterer
   `file_view_stats`. SQL-en skal samtidig kreve at `files.id` finnes og har
   `deleted_at IS NULL`.
3. Implementer en read-only-hjelper som velger tilfeldig kvalifisert fil-ID:
   først usette aktive bilder/videoer, deretter den eldste poolen. Gjenbruk
   `media_kind()` for avgrensning til bilder og videoer, og gjør selve
   tilfeldighetsvalget injiserbart eller på annen måte testbart uten flakete
   tester.
4. Test hjelpefunksjonene isolert, inkludert første/gjentatt visning,
   fjernet eller ukjent fil, `remove`/`undelete`, unimport med og uten
   gjenstående `file_sources`, og sletting av `files`-raden.

### 3. Legg til serverruter

1. Legg `respond_item_viewed()` i `bildebank/server_endpoints_items.py` og
   koble `POST /api/item-viewed` i `BildebankRequestHandler.do_POST`.
   Gjenbruk den etablerte JSON-lesingen, `require_int()` og CSRF-grensen.
2. Bruk en kortvarig SQLite-skrivetilkobling med null eller svært liten
   timeout. Kontroller target-låsen før skriving; ved target-lås eller
   SQLite `busy`/`locked` returneres `204 No Content` uten oppdatering.
   Ikke overta, vent på eller bryt en target-lås.
3. La vanlige valideringsfeil returnere korrekt 400-respons. La uventede
   databasefeil følge serverens ordinære feilbehandling, slik at de fortsatt
   blir synlige i logg og test.
4. Legg `GET /random` i browserruting med en liten
   `respond_random_item()`-adapter. Adapteren bruker databasehjelperen,
   redirecter til `/item/<id>` og returnerer en vanlig tomtilstand når ingen
   kvalifiserte filer finnes. GET må ikke skrive visningsstatistikk.
5. Legg til handler- og endepunktstester for normal registrering,
   ugyldig/ukjent/fjernet ID, target-lås, SQLite-konflikt, read-only og
   tilfeldig redirect/tomt utvalg.

### 4. Koble på browseren og klienten

1. La `source_item_page_html()` sette data-attributtet for
   visningsregistrering bare i skrivbar modus. Kontroller at det samme virker
   for alle felles browserutvalg (alle bilder, person, import og filter), men
   ikke for rutenett eller slideshow.
2. Utvid `bildebank/assets/server.js` med én liten, isolert
   visningsregistrering som bruker den eksisterende `csrfFetch`-hjelperen.
   Den skal håndtere allerede lastede bilder, synlighet, sideforlatelse og
   én-gangsregistrering.
3. Implementer videosporet med mediahendelsene beskrevet over: tell bare
   gyldige `currentTime`-intervaller, nullstill referansepunkt ved seeking og
   krev `ended` med dekket varighet for korte videoer. Legg klienttester eller
   smale DOM-/kildetester for bilde-, video-, seek- og synlighetsreglene.
4. Legg lenken «Tilfeldig bilde» i den felles browserheaderen gjennom
   `server_shell.py`, slik at den blir lik i vanlige browservisninger uten å
   lage en parallell navigasjon. Test at lenken finnes der den skal.

### 5. Verifiser hele endringen

1. Kjør først de nye og berørte testfilene uten xdist, spesielt
   migrerings-, database-, browser- og item-endepunktstester.
2. Kjør deretter `python -m pytest -n auto` og relevante statiske kontroller
   som prosjektet bruker.
3. Gjør en kort manuell kontroll mot en ny eller disponibel test-samling:
   åpne et bilde i to sekunder, spill en lang video i åtte sekunder, spill en
   kort video til slutten, prøv seeking, og åpne `/random`. Ikke bruk en
   primær bildesamling til den første kontrollen.

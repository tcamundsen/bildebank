# Plan for oppsplitting av server

## Konklusjon og formål

`bildebank/server.py` bør fortsatt deles opp. Begrunnelsen er ikke bare at
filen er 2722 linjer, men at den har flere uavhengige grunner til å endres:

- oppstart og sikker binding av HTTP-serveren
- servertilstand og cache for browsernavigasjon
- HTTP-livssyklus, read-only-kontroll, CSRF-kontroll og ruting
- visning av den felles bildebrowseren og ulike browserutvalg
- endring av innstillinger, steder og tag-definisjoner
- visning og endring av personer og ansikter
- handlinger på enkeltbilder, blant annet rotasjon, dato, sted, tag og remove

Disse ansvarene kan endres og testes hver for seg. En målrettet oppsplitting
vil derfor gjøre videre utvikling enklere. Vi skal ikke lage én modul per URL,
flytte små hjelpefunksjoner vilkårlig eller dele eksisterende domenemoduler
bare for å få lavere linjetall.

Målet er å gjøre `server.py` til et stabilt, tynt inngangspunkt og samle
HTTP-endepunktene i noen få moduler med tydelige domenegrenser. Det skal ikke
være noen tilsiktet endring i URL-er, HTTP-metoder, statuskoder, HTML, JSON,
sikkerhet, databaseoppførsel eller filbehandling.

Denne filen er fasit for arbeidet. Status, avvik og viktige beslutninger skal
oppdateres her underveis.

## Status og baseline

- Aktiv branch ved oppstart: `ferie`
- Baseline-commit: `7a85652`
- `bildebank/server.py`: 2722 linjer
- Eksisterende `bildebank/server_*.py`: 20 moduler og 11839 linjer
- pytest: 765 tester og 163 subtester består
- Ruff lint: ingen funn med standardreglene
- pyflakes: ingen funn i `bildebank` eller `tests`
- mypy: ingen funn i 79 kildefiler

| Trinn | Status | Commit |
|---|---|---|
| 0. Opprett plan og baseline | ferdig | `44c9ad8` |
| 1. Trekk ut endepunkter for felles bildebrowser | ferdig, ikke commitet | — |
| 2. Trekk ut admin- og innstillingsendepunkter | ikke startet | — |
| 3. Trekk ut person- og ansiktsendepunkter | ikke startet | — |
| 4. Trekk ut handlinger på enkeltbilder | ikke startet | — |
| 5. Trekk ut HTTP-handler og ruting | ikke startet | — |
| 6. Trekk ut server-runtime og gjør `server.py` tynt | ikke startet | — |
| 7. Avsluttende opprydding og utviklerdokumentasjon | ikke startet | — |

Neste trinn etter at trinn 1 er gjennomgått og commitet: **Trinn 2.**

## Datamodell som skal ligge fast

Gjeldende runtime-schema er v14. `files` og `file_sources` har ulike ansvar:

- `files` beskriver den ene lagrede bildefilen i samlingen, med blant annet
  `target_path`, SHA-256, metadata og eventuell `deleted_at`.
- `file_sources` beskriver hvor en fil ble observert i en importkilde.
  `file_sources.file_id` peker til `files.id`.
- Flere `file_sources`-rader kan peke til samme `files`-rad. Dette er nødvendig
  når samme innhold finnes i flere kilder.
- Kombinasjonen `(source_id, source_path_key)` er unik, mens `file_id` ikke er
  unik i `file_sources`.

Oppsplittingen skal ikke endre schema, SQL-semantikk eller denne relasjonen.
Et importert kildeutvalg skal fortsatt bygges gjennom den felles
`BrowserSource`- og browserflyten. Det skal ikke innføres en parallell browser
som antar at én `files`-rad bare har én kilde.

## Regler for hele refaktoreringen

1. Ingen tilsiktet endring i produktoppførsel, URL-er, HTTP-metoder,
   statuskoder, responsformat, sikkerhet eller filbehandling skal blandes inn.
2. `read_only`, CSRF-kontroll, lokal bind som standard og kravet om eksplisitt
   `--allow-remote` skal bevares og testes etter alle relevante trinn.
3. Handlinger som endrer samlingen skal fortsatt bruke eksisterende
   domenefunksjoner og target-lås. Ingen filhandling skal flyttes inn i
   transportlaget.
4. `remove` skal fortsatt flytte filen til `deleted/` og markere metadata. En
   refaktorering av serveren skal aldri innføre fysisk sletting av bildefiler.
5. Den felles browserflyten i `app-design.md` skal bevares. Person-, kilde-,
   tag-, sted- og filterutvalg skal fortsatt bruke `BrowserSource` og felles
   navigasjon.
6. Ikke bruk mixin-klasser for å dele `BildebankRequestHandler`. Domenevise
   endepunkter skal være vanlige funksjoner som får handleren eksplisitt.
7. Ikke innfør et generisk web-rammeverk eller en ny abstrakt router i denne
   refaktoreringen. Den eksplisitte ruterekkefølgen skal være lett å lese.
8. Midlertidige delegasjonsmetoder på `BildebankRequestHandler` kan beholdes
   mens kode og tester flyttes. De fjernes når alle interne kallesteder bruker
   den nye eiermodulen.
9. Ikke del eksisterende store domenemoduler som `server_faces.py`,
   `server_filter.py` eller `server_browser_queries.py` som del av denne planen.
   De vurderes separat bare hvis konkret videre arbeid viser en naturlig
   ansvarsgrense.
10. Ikke del testfiler bare for å speile produksjonsfilene. Flytt eller del
    tester bare når det gir tydeligere eierskap eller fjerner direkte kobling
    til den gamle handlerklassen.
11. Windows 11 er hovedplattformen. Nye modulgrenser skal ikke bygge inn
    Linux-spesifikke antakelser.
12. Hvis et trinn avdekker behov for endret produktoppførsel, stoppes
    refaktoreringen. Endringen beskrives og vurderes som en separat oppgave.

## Målstruktur og avhengighetsretning

Dette er en styrende skisse, ikke et krav om nøyaktige filnavn eller linjetall.
Hvis arbeidet viser at to foreslåtte moduler naturlig hører sammen, skal de
heller beholdes sammen enn å oppfylle tabellen mekanisk.

| Modul | Ansvar | Omtrentlige linjer |
|---|---|---:|
| `server.py` | Stabilt offentlig inngangspunkt og re-eksporter som faktisk trengs | 20–60 |
| `server_runtime.py` | Bind-validering, `BildebankServer`, servertilstand, navigasjonscache og `run_server()` | 300–420 |
| `server_handler.py` | `BaseHTTPRequestHandler`, request-livssyklus, read-only, CSRF, GET/POST-ruting og generelle ressurser | 500–750 |
| `server_endpoints_browser.py` | Felles browser, dato-, filter-, kilde-, tag- og stedutvalg | 400–600 |
| `server_endpoints_admin.py` | Innstillinger, navngitte H3-celler, egendefinerte steder og tag-definisjoner | 250–400 |
| `server_endpoints_faces.py` | Personvisninger, ansiktsforslag og person-/ansiktsendringer | 300–450 |
| `server_endpoints_items.py` | Rotasjon, tag, manuelt sted, manuell dato, hotkeys, remove og undelete | 400–550 |

Eksisterende moduler skal fortsatt eie domenelogikken. De nye
`server_endpoints_*`-modulene er HTTP-adaptere: De leser request-data, kaller
for eksempel `server_actions`, `server_geo`, `server_faces` og
`server_browser_queries`, og skriver en HTTP-respons.

Ønsket runtime-avhengighet er:

```text
server
  -> server_runtime
       -> server_handler
            -> server_endpoints_*
                 -> eksisterende domene-, query- og HTML-moduler
```

Endepunktmodulene skal ikke importere `server.py` ved runtime. Hvis de trenger
handlerklassen for typeannotasjoner, brukes en `TYPE_CHECKING`-import eller et
lite eksplisitt protocol dersom dette viser seg nødvendig. Det skal ikke være
sirkulære runtime-importer.

## Fast kontroll etter hvert trinn

Kjør fra aktivert `.venv`:

```powershell
python -m pytest -q
python -m ruff check bildebank tests
python -m pyflakes bildebank tests
python -m mypy bildebank
git diff --check
```

Forventning:

- pytest skal være grønn.
- Ruff lint skal være grønn.
- pyflakes skal være grønn.
- mypy skal være grønn.
- Ingen nye sirkulære importer skal finnes.

Etter trinn som endrer handler, ruting eller runtime skal serveren i tillegg
startes mot en liten testsamling. Kontroller minst rotvisning, én
enkeltbildeside, én statisk ressurs og normal avslutning. På Windows skal den
vanlige launcherflyten for å starte og stoppe serveren prøves før neste
versjon.

## Trinn 0 – Opprett plan og baseline

### Arbeid

- Hent og kontroller runtime-schemaet.
- Les `app-design.md` og den fullførte planen for launcher-oppsplittingen.
- Registrer branch, commit, filstørrelser og teststatus.
- Beskriv målstruktur, sikkerhetskrav og trinnvis arbeidsflyt.

### Ferdigkriterium

- Planen kan følges uten tidligere samtalehistorikk.
- Det er forklart hvorfor oppsplittingen gir tydeligere ansvar.
- Det er uttrykkelig beskrevet hva som ikke skal endres.

### Brukerens oppgave

- Les planen og kontroller spesielt modulgrensene.
- Commit planfilen alene, foreslått melding:

```text
Planlegg oppsplitting av server
```

## Trinn 1 – Trekk ut endepunkter for felles bildebrowser

### Arbeid

- Opprett `bildebank/server_endpoints_browser.py`.
- Flytt handlerlogikk for rotbrowser, enkeltbilde, måned, år, filter,
  importert kilde, tag og geografiske browserutvalg.
- Behold all utvalgslogikk i eksisterende `BrowserSource`-, query- og
  HTML-moduler. Den nye modulen skal bare koordinere HTTP-request og respons.
- Behold midlertidige delegasjonsmetoder på `BildebankRequestHandler`, slik at
  flyttingen kan gjennomgås uten samtidig å skrive om alle tester.
- Legg til eller styrk en test som viser at person-, kilde-, tag-, sted- og
  filterutvalg går gjennom den samme `respond_browser_source`-flyten.

### Ferdigkriterium

- Browsernavigasjon, URL-er, sideinnhold og cacheinvalidering er uendret.
- Kildeutvalg beholder mange-til-én-forholdet mellom `file_sources` og
  `files`.
- Ingen SQL eller domenelogikk er kopiert inn i endepunktmodulen.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Prøv rotbrowser, år/måned, én importert kilde og ett filter mot en liten
  testsamling.
- Commit foreslått melding:

```text
Trekk browser-endepunkter ut av server
```

### Resultat

- `server_endpoints_browser.py` er opprettet med 543 linjer og eier
  HTTP-adapterne for rotbrowser, enkeltbilde, måned, år, filter, importert
  kilde, tag og geografiske browserutvalg.
- `BildebankRequestHandler` beholder midlertidige delegasjonsmetoder, slik at
  ruting og eksisterende interne kallesteder er uendret i overgangsperioden.
- `server.py` er redusert fra 2722 til 2291 linjer.
- En ny kontrakttest viser at person-, kilde-, tag-, filter- og stedsutvalg
  går gjennom den samme `respond_browser_source`-flyten.
- Tester som patcher queryfunksjoner, patcher nå navnet i modulen som faktisk
  slår det opp. Det er ikke gjort endringer i SQL eller domenelogikk.
- 766 tester og 163 subtester består.
- Ruff, pyflakes, mypy og `git diff --check` er grønne.

## Trinn 2 – Trekk ut admin- og innstillingsendepunkter

### Arbeid

- Opprett `bildebank/server_endpoints_admin.py`.
- Flytt request-/responslogikk for serverinnstillinger, hotkeys, H3-navn,
  egendefinerte steder og oppretting, endring og sletting av tag-definisjoner.
- La configskriving fortsatt eies av `server_app`/`config`, tag-endringer av
  `file_tags` og stedlogikk av `server_geo`.
- Behold eksisterende redirect med scroll-posisjon og samme valideringsfeil.
- Behold midlertidige handler-delegater til testene er flyttet.

### Ferdigkriterium

- CSRF, read-only-blokkering, target-lås og statuskoder er uendret.
- Endringer av tag-definisjoner invaliderer fortsatt bare nødvendig
  browsercache.
- Ingen konfigurasjons- eller databaseoppdatering er duplisert i
  transportlaget.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Prøv én ufarlig innstillingsendring i en testsamling og sett den tilbake.
- Kontroller at read-only-serveren fortsatt avviser adminsidene og alle POST.
- Commit foreslått melding:

```text
Trekk admin-endepunkter ut av server
```

## Trinn 3 – Trekk ut person- og ansiktsendepunkter

### Arbeid

- Opprett `bildebank/server_endpoints_faces.py`.
- Flytt personvisning, referansevisning, forslag om manglende referanser og
  kjøring av ansiktsforslag.
- Flytt HTTP-adapterne for å legge til/fjerne ansikt eller fil, opprette,
  endre navn på og slette person.
- La all ansikts- og personlogikk fortsatt eies av `face.py` og
  `server_faces.py`.
- Behold feature-flag-kontroll, cachetømming, redirect og payloadformat
  uendret.

### Ferdigkriterium

- Endepunktene finnes bare når ansiktsgjenkjenning er aktivert, som før.
- JSON-felter, feilmeldinger, statuskoder og personlenker er uendret.
- Ingen ansiktsdatabaseoperasjon er implementert på nytt i endepunktmodulen.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Prøv personside og referanseside i en testsamling med ansiktsdata.
- En faktisk personendring er ikke nødvendig bare for refaktorering dersom
  de automatiske testene dekker flyten.
- Commit foreslått melding:

```text
Trekk person- og ansiktsendepunkter ut av server
```

## Trinn 4 – Trekk ut handlinger på enkeltbilder

### Arbeid

- Opprett `bildebank/server_endpoints_items.py`.
- Flytt HTTP-adapterne for rotasjon, tag, manuelt sted, manuell dato,
  hotkey-handling, remove og undelete.
- Flytt de små hjelpefunksjonene som beregner nabobilde og redirect etter en
  endring når de bare brukes av disse endepunktene.
- La fil- og databaseendringer fortsatt utføres av `server_actions` og
  underliggende livssyklusmoduler med target-lås.
- Test spesielt at et bilde som forsvinner ut av et aktivt filter etter en
  endring, fortsatt sender browseren til riktig nabobilde.

### Ferdigkriterium

- Ingen endring kan omgå CSRF, read-only eller target-lås.
- `remove` flytter fortsatt til `deleted/`; ingen permanent sletting finnes.
- `undelete`, manuell dato, manuelt sted, tag og rotasjon har identiske
  payloads og statuskoder.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Bruk bare en testsamling ved manuell kontroll.
- Prøv en reverserbar handling, for eksempel rotasjon eller tag.
- Hvis remove prøves manuelt, kontroller at filen ligger i `deleted/` og kan
  gjenopprettes med undelete.
- Commit foreslått melding:

```text
Trekk bildehandlinger ut av server
```

## Trinn 5 – Trekk ut HTTP-handler og ruting

### Arbeid

- Opprett `bildebank/server_handler.py` med `BildebankRequestHandler`.
- Flytt request-livssyklus, databaseforbindelse per request,
  klientfrakobling, logging, timing, read-only-kontroll, CSRF og eksplisitt
  GET/POST-ruting.
- La rutingen kalle funksjonene i `server_endpoints_*` direkte.
- Behold generelle ressurser som statiske assets, hjelpefiler,
  bildedisplay/preview, lazy item-info og vedlikeholdsstatus i handleren så
  lenge de ikke danner en stor selvstendig domenegruppe.
- Fjern midlertidige delegasjonsmetoder og oppdater direkte enhetstester til å
  importere funksjonen fra eiermodulen. Behold integrasjonstester gjennom den
  virkelige handlerklassen.
- Legg til en kompakt rutekontrakttest for representative GET- og POST-ruter,
  inkludert ukjent endepunkt og fallback til `/file/`.

### Ferdigkriterium

- Rekkefølgen på ruter og fallback-oppførsel er uendret.
- `handle()` lukker fortsatt requestens databaseforbindelse også ved feil.
- Read-only blokkerer samme GET-sider og alle POST som før.
- CSRF-body kan fortsatt leses av endepunktet etter validering.
- Ingen endpointmodul importerer handleren ved runtime.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Start serveren mot en testsamling og åpne rot, settings, ett bilde og en
  dokumentasjonsside.
- Commit foreslått melding:

```text
Trekk HTTP-handler ut av server
```

## Trinn 6 – Trekk ut server-runtime og gjør server.py tynt

### Arbeid

- Opprett `bildebank/server_runtime.py`.
- Flytt bind-validering, `BildebankServer`, serveregenskaper,
  browsernavigasjonscache og `run_server()` dit.
- Behold cache og servertilstand samlet i `BildebankServer`. Trekk ikke ut en
  egen cacheklasse med mindre flyttingen viser et konkret behov; rundt 350
  sammenhengende linjer er akseptabelt.
- Gjør `bildebank/server.py` til et tynt offentlig inngangspunkt som minst
  eksponerer `run_server`. Behold re-eksport av `BildebankServer` og
  `BildebankRequestHandler` bare dersom interne eller dokumenterte
  kallesteder trenger dem.
- Oppdater `cli_server.py` og tester til å importere fra den egentlige
  eiermodulen der det er riktig.
- Kontroller at patching i tester skjer på stedet navnet slås opp, ikke via
  tilfeldige re-eksporter.

### Ferdigkriterium

- `server.py` er et tydelig og stabilt inngangspunkt på omtrent 20–60 linjer.
- Runtime-avhengigheten går i én retning uten sirkulære importer.
- Oppstart, ready-callback, faktisk port ved `port=0`, advarsler ved remote
  bind og kontrollert `server_close()` er uendret.
- Browsernavigasjonscache har samme invalidasjon og mtime-throttling som før.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Start serveren med den vanlige `bildebank run-server`-kommandoen.
- Kontroller normal start/stopp og launcherens serverknapp på Windows.
- Commit foreslått melding:

```text
Gjør server til et tynt inngangspunkt
```

## Trinn 7 – Avsluttende opprydding og utviklerdokumentasjon

### Arbeid

- Kontroller endelige modulstørrelser og avhengighetsretning.
- Fjern overgangsdelegater og re-eksporter som ikke lenger trengs.
- Kontroller at testene tester oppførsel og eiermoduler, ikke Python-kilde som
  tekst, med mindre en slik test har en tydelig begrunnelse.
- Opprett eller oppdater en kort utvikleroversikt over servermodulene dersom
  målstrukturen trenger mer forklaring enn denne planen.
- Oppdater status, faktiske resultater, commit-hasher og eventuelle avvik i
  denne filen.

### Ferdigkriterium

- Hele kontrollsettet består.
- Ingen sirkulære runtime-importer eller foreldreløse overgangs-API-er finnes.
- `server.py` inneholder bare offentlig inngang og nødvendig kompatibilitet.
- Windows-smoketest er utført eller registrert som et tydelig gjenstående
  punkt før neste versjon.

### Brukerens oppgave

- Gjør en siste kontroll på Windows 11 med en testsamling: start via launcher,
  bla mellom bilder, bruk ett filter, åpne innstillinger, utfør én reverserbar
  bildehandling og stopp serveren normalt.
- Commit foreslått melding:

```text
Fullfør oppsplitting av server
```

## Arbeidsflyt mellom hvert trinn

1. Brukeren ber om neste nummererte trinn.
2. Arbeidstreet kontrolleres. Urelaterte lokale endringer røres ikke.
3. `app-design.md` og relevante utviklerdokumenter leses på nytt hvis trinnet
   berører produktatferd, database eller filhandlinger.
4. Trinnet markeres `pågår` i denne filen.
5. Kode og tester endres bare innenfor trinnets omfang.
6. Fokuserte tester kjøres først, deretter hele kontrollsettet.
7. Resultat, beslutninger, filstørrelser og neste trinn skrives inn her.
8. Brukeren gjennomgår diffen og gjør commit.
9. Ved starten av neste trinn registreres forrige commit-hash i statustabellen.

Hvis en ny samtale eller en omstart skjer, skal arbeidet fortsette fra
statusfeltet og «Neste trinn» i denne filen, ikke rekonstrueres fra minnet.

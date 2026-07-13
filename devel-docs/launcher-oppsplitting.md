# Plan for oppsplitting av launcher

## Formål

`bildebank/launcher.py` har vokst til 2955 linjer. Modulen inneholder i dag
datamodeller, kommandobygging, miljøkontroll, Tk-widgets, alle fanene i
launcheren, bakgrunnstråder og subprocess-håndtering.

Målet er å dele dette i mindre moduler med tydelige ansvar, uten tilsiktede
endringer i produktoppførsel. Oppdelingen skal gjøres i små, testbare steg og
med én commit per steg.

Denne filen er fasit for arbeidet. Status, avvik fra planen og viktige
beslutninger skal oppdateres her underveis.

## Status og baseline

- Aktiv branch ved oppstart: `ferie`
- Baseline-commit: `94e324e`
- `bildebank/launcher.py`: 2955 linjer
- `tests/test_launcher.py`: 1539 linjer
- pytest: 744 tester og 148 subtester består
- Ruff lint: ingen funn med standardreglene
- pyflakes: ingen funn i `bildebank` eller `tests`
- mypy: ingen funn i 70 kildefiler etter stabiliseringsrunden

| Trinn | Status | Commit |
|---|---|---|
| 0. Opprett plan og baseline | ferdig | `2d2310b` |
| 0A. Stabiliser utviklerverktøy | ferdig | `4fef0e8` |
| 1. Trekk ut kommandobyggere | ferdig | `c2ec28a` |
| 2. Trekk ut status og miljøkontroll | ferdig | `f4fe0fc` |
| 3. Trekk ut prosesskjøring | ferdig | `f7a0abf` |
| 4. Trekk ut generelle widgets og dialoger | ferdig | `1b9e309` |
| 5. Trekk ut Oppsett-fanen | ferdig | `3b6648c` |
| 6. Trekk ut Import-fanen | ferdig | `c213343` |
| 7. Trekk ut Verktøy-fanen | ferdig | `559a551` |
| 8. Trekk ut hovedfanen | ferdig, ikke committet | |
| 9. Gjør `launcher.py` til et tynt inngangspunkt | ikke startet | |
| 10. Avsluttende testopprydding og dokumentasjon | ikke startet | |

Neste trinn: **9. Gjør `launcher.py` til et tynt inngangspunkt.**

## Regler for hele refaktoreringen

1. Ingen tilsiktet endring i synlig oppførsel, kommandolinjer, sikkerhet eller
   filbehandling skal blandes inn i oppsplittingen.
2. Ikke gjør generell opprydding utenfor koden som flyttes i det aktuelle
   trinnet.
3. Behold midlertidige re-eksporter fra `bildebank.launcher` når det gjør
   overgangen mindre risikabel. Fjern dem først i trinn 9 når alle kallesteder
   er flyttet.
4. Bruk komposisjon mellom appen, fanene og prosesskjøreren. Ikke del
   `BildebankLauncher` opp i mixin-klasser med skjult delt tilstand.
5. Tk-kall skal fortsatt skje på Tk-tråden. Bakgrunnstråder skal levere
   resultater tilbake gjennom den eksisterende `root.after`-mekanismen eller
   en tilsvarende eksplisitt mekanisme.
6. Windows 11 er hovedplattformen. Nye modulgrenser må ikke bygge inn
   Linux-spesifikke antakelser.
7. Etter hvert trinn skal relevante tester, hele pytest-samlingen, Ruff,
   pyflakes og mypy kjøres før commit.
8. Hvis et trinn avdekker behov for endret produktoppførsel, stoppes
   refaktoreringen. Endringen vurderes som en separat oppgave og commit.

## Målstruktur og omtrentlige størrelser

Dette er et mål, ikke et krav om nøyaktig antall linjer.

| Modul | Ansvar | Omtrentlige linjer |
|---|---|---:|
| `launcher.py` | Offentlig inngangspunkt og `main()` | 30–70 |
| `launcher_commands.py` | Bygging av CLI- og installasjonskommandoer | 250–320 |
| `launcher_status.py` | Datamodeller, config, Git-, migrerings- og AI-status | 300–400 |
| `launcher_runner.py` | Subprocess, avbrytelse, tråder og progresjonslogg | 220–300 |
| `launcher_widgets.py` | Tooltip og gjenbrukbare dialoger/velgere | 200–300 |
| `launcher_app.py` | Rotvindu, notebook, felles status, logg og koordinering | 300–450 |
| `launcher_main_tab.py` | Samling, server, oppdatering, backup og migrering | 350–450 |
| `launcher_import_tab.py` | Import, rescan, check-source og unimport | 450–600 |
| `launcher_tools_tab.py` | Scanner, browserbygging, doctor, vacuum og eksport | 550–700 |
| `launcher_setup_tab.py` | InsightFace/OpenCLIP-installasjon og modellstatus | 300–400 |

Fanene skal få eksplisitt tilgang til et lite felles grensesnitt for
eksempel `run_command`, `log`, `set_busy` og `refresh`. De skal ikke kjenne til
all intern tilstand i `LauncherApp`.

## Fast kontroll etter hvert trinn

Kjør fra aktivert `.venv`:

```powershell
python -m pytest -q
python -m ruff check bildebank tests
python -m pyflakes bildebank tests
python -m mypy bildebank
```

Forventning:

- pytest skal være grønn.
- Ruff lint skal være grønn.
- pyflakes skal være grønn.
- mypy skal være grønn etter trinn 0A.
- `git diff --check` skal være grønn.

`ruff format --check` inngår ikke i kontrollsettet nå. Ruff sitt
standardformat ville omskrevet 112 eksisterende filer. En eventuell felles
formatteringsstandard skal derfor vurderes som en separat oppgave og commit,
ikke blandes inn i launcher-oppsplittingen.

Ved trinn som flytter Tk-grensesnitt skal launcheren i tillegg startes manuelt.
Kontroller at vinduet åpner, at alle faner vises, og at vinduet kan avsluttes.
På Windows bør minst én knapp i den berørte fanen prøves med et lite
testoppsett før commit.

## Trinn 0 – Opprett plan og baseline

### Arbeid

- Registrer filstørrelser, branch og commit.
- Kjør pytest, pyflakes og mypy eller registrer siste sikre resultat.
- Opprett denne planen.

### Ferdigkriterium

- Planen er forståelig uten tilgang til tidligere samtalehistorikk.
- Eksisterende analysefeil er skilt fra feil som refaktoreringen kan innføre.

### Brukerens oppgave

- Les planen og kontroller at modulgrensene virker fornuftige.
- Commit planfilen alene, foreslått melding:

```text
Planlegg oppsplitting av launcher
```

## Trinn 0A – Stabiliser utviklerverktøy

### Arbeid

- Registrer Ruff som utviklingsavhengighet.
- Kjør Ruff med standardreglene og avklar formatteringsomfanget.
- Rett de fire eksisterende mypy-feilene i `server_faces.py` uten å endre
  oppførsel.
- Kjør hele kontrollsettet og etabler en grønn baseline før launcher-kode
  flyttes.

### Ferdigkriterium

- pytest, Ruff lint, pyflakes og mypy er grønne.
- Ruff-formatering er uttrykkelig holdt utenfor launcher-refaktoreringen.
- Ingen launcher-kode er flyttet ennå.

### Brukerens oppgave

- Commit utvikleravhengigheten og den avgrensede mypy-rettingen separat fra
  selve launcher-oppsplittingen, foreslått melding:

```text
Stabiliser statisk analyse før launcher-oppsplitting
```

## Trinn 1 – Trekk ut kommandobyggere

### Arbeid

- Opprett `bildebank/launcher_commands.py`.
- Flytt de rene funksjonene som bygger kommandoargumenter, inkludert
  `bildebank_command`, `create_command`, import-, scan-, browser-, backup-,
  unimport- og installasjonskommandoene.
- Flytt lesing av unimport target-change-rapport hvis det gir en ren
  avhengighetsretning.
- Behold re-eksporter fra `bildebank.launcher` i dette trinnet.
- Flytt de tilhørende testene til `tests/test_launcher_commands.py`.

### Ferdigkriterium

- Kommandobygging kan testes uten å opprette Tk-vindu.
- Ingen kommandolinje har endret innhold eller rekkefølge.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Se spesielt på diffen for kommandoargumentene.
- Kontroller at ingen flagg eller argumentrekkefølger er endret.
- Commit foreslått melding:

```text
Trekk launcher-kommandoer ut i egen modul
```

### Resultat

- `launcher_commands.py` er opprettet med 177 linjer.
- `launcher.py` er redusert fra 2955 til 2818 linjer.
- Fire kommandotester er flyttet til `test_launcher_commands.py`, og
  `run-server`-kommandoen har fått en eksplisitt kontroll.
- Gjenværende kommandonavn som launcheren bruker er fortsatt tilgjengelige
  fra `bildebank.launcher` i overgangsperioden.
- 744 tester og 148 subtester består.
- Ruff, pyflakes og mypy er grønne.

## Trinn 2 – Trekk ut status og miljøkontroll

### Arbeid

- Opprett `bildebank/launcher_status.py`.
- Flytt status-dataklasser og funksjoner for launcher-config, Git-oppdatering,
  migreringsbehov, registrerte kilder/personer og InsightFace/OpenCLIP-status.
- Behold UI-oppdatering av etiketter og knapper utenfor statusmodulen.
- Flytt tilhørende tester til `tests/test_launcher_status.py`.

### Ferdigkriterium

- Statusmodulen returnerer data og utfører ikke Tk-operasjoner.
- Feiltekster og statusverdier er uendret.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Se at statuskontroll fortsatt er read-only bortsett fra den eksisterende
  `git fetch`-oppdateringssjekken.
- Start launcheren og se at Oppsett-status og oppdateringsknapp fortsatt
  oppdateres.
- Commit foreslått melding:

```text
Trekk launcher-status ut i egen modul
```

### Resultat

- `launcher_status.py` er opprettet med 266 linjer og utfører ingen
  Tk-operasjoner.
- `launcher.py` er redusert videre fra 2818 til 2577 linjer.
- 32 config-, Git-, migrerings-, kilde-, person- og AI-statustester er flyttet
  til `test_launcher_status.py`.
- Git-oppdateringssjekkens eksisterende `git fetch` er uendret; øvrige
  statuskontroller er read-only.
- 744 tester og 148 subtester består.
- Ruff, pyflakes og mypy er grønne.

## Trinn 3 – Trekk ut prosesskjøring

### Arbeid

- Opprett `bildebank/launcher_runner.py` med en eksplisitt `CommandRunner`.
- Flytt oppstart, output-lesing, progresjonsgjenkjenning, ferdig-callback,
  avbrytelse og plattformspesifikk signalhåndtering.
- `CommandRunner` skal kommunisere gjennom callbacks og ikke importere
  launcher-fanene.
- Legg prosess- og avbruddstester i `tests/test_launcher_runner.py`.

### Ferdigkriterium

- Bare runneren eier den aktive subprocessen.
- Ctrl-C/CTRL_BREAK-semantikk og kontrollert avbrytelse er uendret.
- Tk oppdateres ikke direkte fra worker-tråden.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Start en ufarlig, langvarig jobb på et testoppsett og prøv «Avbryt jobb».
- Kontroller at loggen oppdateres og at knappene aktiveres igjen.
- Gjør denne manuelle kontrollen på Windows før neste versjon dersom den ikke
  kan gjøres nå.
- Commit foreslått melding:

```text
Trekk launcher-prosesskjøring ut i egen modul
```

### Resultat

- `launcher_runner.py` er opprettet med 181 linjer.
- `CommandRunner` eier aktiv subprocess, cancellable-status og
  avbruddsforespørsel.
- Worker-tråden leverer fortsatt output og sluttstatus via UI-callback;
  runneren utfører ingen Tk-kall.
- `launcher.py` er redusert videre fra 2577 til 2453 linjer.
- Prosess- og progresjonstester er flyttet til `test_launcher_runner.py`, og
  to nye tester dekker cancellation-state og callback-flyten.
- 746 tester og 148 subtester består.
- Ruff, pyflakes og mypy er grønne.

## Trinn 4 – Trekk ut generelle widgets og dialoger

### Arbeid

- Opprett `bildebank/launcher_widgets.py`.
- Flytt `Tooltip`, strengdialog og generelle kilde-/personvelgere når de kan
  uttrykkes uten å kjenne hele launcher-objektet.
- Dialogene skal få parent, data og callbacks eksplisitt.
- Erstatt kildekodeinspeksjon i tester med tester av oppførsel der det er
  praktisk.

### Ferdigkriterium

- Widgets importerer ikke `LauncherApp` eller fanemodulene.
- Modal/nonmodal oppførsel er uendret.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Åpne dialogene for valg av kilde og person, og kontroller Avbryt/OK.
- Se at tooltip fortsatt vises og forsvinner normalt.
- Commit foreslått melding:

```text
Trekk launcher-widgets ut i egen modul
```

### Resultat

- `launcher_widgets.py` er opprettet med 262 linjer.
- `Tooltip`, strengdialogen, loggvurderingsdialogen og velgerne for kilde og
  person er flyttet med eksplisitte avhengigheter og callbacks.
- `launcher.py` beholder tynne delegasjonsmetoder og er redusert videre fra
  2453 til 2229 linjer.
- Fire dialogtester er flyttet til `test_launcher_widgets.py` og tester de nye
  eierfunksjonene.
- 746 tester og 148 subtester består.
- Ruff, pyflakes og mypy er grønne.

## Trinn 5 – Trekk ut Oppsett-fanen

### Arbeid

- Opprett `bildebank/launcher_setup_tab.py`.
- La et `SetupTab`-objekt bygge fanen, vise avhengighetsstatus og håndtere
  installasjon/nedlasting av InsightFace og OpenCLIP.
- Skille mellom statusinnhenting i `launcher_status.py` og visning/handling i
  fanen.
- Flytt relevante tester til `tests/test_launcher_setup_tab.py`.

### Ferdigkriterium

- Knappetekst, enabled/disabled-status og installasjonssekvens er uendret.
- Asynkron statusinnhenting blokkerer ikke oppstart av vinduet.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Start launcheren og kontroller status for InsightFace, ansiktsmodell,
  OpenCLIP og OpenCLIP-modell.
- Ikke installer store modeller bare for refaktoreringstesten; kontroller
  faktisk installasjonsflyt på Windows når det passer.
- Commit foreslått melding:

```text
Trekk Oppsett-fanen ut av launcher
```

### Resultat

- `launcher_setup_tab.py` er opprettet med 356 linjer.
- `SetupTab` bygger Oppsett-fanen og eier status, statusoppdatering,
  knappetilstand, installasjon av InsightFace/OpenCLIP og modellnedlasting.
- Statusinnhenting kjører fortsatt i en bakgrunnstråd og leverer resultatet
  tilbake gjennom launcherens UI-callback.
- Face- og image-scan bruker et eksplisitt grensesnitt mot `SetupTab` for
  status og installasjonstrinn.
- `launcher.py` er redusert videre fra 2229 til 1958 linjer.
- Seks målrettede Oppsett-tester ligger i `test_launcher_setup_tab.py`.
- 748 tester og 148 subtester består.
- Ruff, pyflakes og mypy er grønne.

## Trinn 6 – Trekk ut Import-fanen

### Arbeid

- Opprett `bildebank/launcher_import_tab.py`.
- Flytt import, rescan-source, check-source og unimport med alle dry-run- og
  bekreftelsessteg.
- Behold sikkerhetsbekreftelser og rapporttolking uendret.
- Flytt relevante tester til `tests/test_launcher_import_tab.py`.

### Ferdigkriterium

- Ingen kommando kan hoppe over eksisterende dry-run eller bekreftelse.
- Kildevalg og håndtering av endrede target-filer er uendret.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Bruk kun en liten testsamling.
- Prøv import og check-source.
- Kontroller unimport dry-run, men avbryt før faktisk unimport med mindre
  testoppsettet uttrykkelig kan kastes.
- Commit foreslått melding:

```text
Trekk Import-fanen ut av launcher
```

### Resultat

- `launcher_import_tab.py` er opprettet med 399 linjer.
- `ImportTab` bygger fanen og eier import, rescan-source, check-source og hele
  unimport-flyten.
- Unimport kjører fortsatt dry-run først, krever nøyaktig tekstbekreftelse og
  krever en ekstra bekreftelse når dry-run rapporterer endrede målfiler.
- Importfanen får samlingssti, kommandokjøring, logg, refresh og dialoghjelpere
  gjennom eksplisitte callbacks.
- `launcher.py` er redusert videre fra 1958 til 1655 linjer.
- Ni målrettede import- og sikkerhetstester ligger i
  `test_launcher_import_tab.py`.
- 755 tester og 148 subtester består.
- Ruff, pyflakes og mypy er grønne.

## Trinn 7 – Trekk ut Verktøy-fanen

### Arbeid

- Opprett `bildebank/launcher_tools_tab.py`.
- Flytt geo-scan, face-scan, image-scan, miniatyrbilder, statiske browsere,
  doctor, vacuum, pending-deletes og eksport av person.
- Setup-preflight for face/image-scan skal bruke et eksplisitt grensesnitt mot
  `SetupTab`, ikke lese tilfeldige interne felt.
- Flytt relevante tester til `tests/test_launcher_tools_tab.py`.

### Ferdigkriterium

- Alle verktøyknapper har samme enabled/disabled-regler og tooltips.
- Face- og image-scan beholder installasjon/aktivering før scan.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Kontroller visuelt at alle verktøyknapper finnes.
- Prøv et raskt verktøy som doctor på en testsamling.
- Face- og image-scan kan nøye seg med preflight-kontroll på den gamle
  laptopen; full Windows-kontroll kan gjøres senere.
- Commit foreslått melding:

```text
Trekk Verktøy-fanen ut av launcher
```

### Resultat

- `launcher_tools_tab.py` er opprettet med 791 linjer.
- `ToolsTab` bygger fanen og eier scannerne, miniatyrbilder, statiske
  browsere, doctor, vacuum, pending-deletes og personeksport.
- Face- og image-scan bruker et eksplisitt, typekontrollert grensesnitt mot
  Oppsett-fanen for status, installasjon og modellnedlasting.
- Alle 13 verktøykontroller opprettes fortsatt bare for en tilgjengelig,
  ferdig migrert bildesamling, og registreres for felles enabled/disabled-
  styring.
- Eksisterende dry-run, bekreftelser, kommandolinjer og avbrytbarhet er
  beholdt.
- `launcher.py` er redusert videre fra 1655 til 1000 linjer.
- 16 målrettede tester ligger i `test_launcher_tools_tab.py`.
- 756 tester og 153 subtester består i det samlede arbeidstreet.
- Ruff, pyflakes og mypy er grønne.

## Trinn 8 – Trekk ut hovedfanen

### Arbeid

- Opprett `bildebank/launcher_main_tab.py`.
- Flytt valg/oppretting av samling, serverstart, oppdatering, backup og
  migreringsdialog.
- La `LauncherApp` eie rotvinduet og koordinere samlet refresh.
- Flytt relevante tester til `tests/test_launcher_main_tab.py`.

### Ferdigkriterium

- Serverprosess stoppes fortsatt kontrollert når launcheren avsluttes.
- Backup beholder dry-run og bekreftelse.
- Migreringskrav blokkerer andre handlinger som før.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Velg en testsamling, start browseren og avslutt launcheren.
- Kontroller backup dry-run mot et testmål; ikke bruk eneste virkelige backup
  som refaktoreringsmål.
- Commit foreslått melding:

```text
Trekk hovedfanen ut av launcher
```

### Resultat

- `launcher_main_tab.py` er opprettet med 606 linjer.
- `MainTab` bygger hovedfanen og eier samlingsvalg, oppretting, migrering,
  serverprosess, oppdateringsstatus og backupflyt.
- Backup beholder dry-run, loggvurdering og eksplisitt bekreftelse før den
  faktiske speilingen kjøres.
- Migreringskrav og feil ved lesing av migreringsstatus blokkerer fortsatt
  import-, verktøy- og oppsettshandlinger.
- Serverprosessen stoppes fortsatt kontrollert ved bytte av samling,
  oppdatering og avslutning av launcheren.
- Hovedfanen kommuniserer med launcher-appen gjennom eksplisitte callbacks;
  appen eier fortsatt rotvindu, busy-status, logg og samlet refresh.
- `launcher.py` er redusert videre fra 1000 til 505 linjer.
- 19 målrettede tester ligger i `test_launcher_main_tab.py`, mens
  `test_launcher.py` er redusert til fem tester av appskallet.
- 762 tester og 163 subtester består i det samlede arbeidstreet.
- Ruff, pyflakes og mypy er grønne.

## Trinn 9 – Gjør launcher.py til et tynt inngangspunkt

### Arbeid

- Flytt gjenværende app-koordinering til `bildebank/launcher_app.py`.
- La `bildebank/launcher.py` bare eksponere stabilt inngangspunkt og `main()`.
- Oppdater interne importer og tester til de nye eiermodulene.
- Fjern midlertidige re-eksporter som ikke er del av nødvendig offentlig API.
- Kontroller at `bildebank start` fortsatt importerer riktig funksjon.

### Ferdigkriterium

- `launcher.py` er omtrent 30–70 linjer.
- Modulavhengighetene går fra app/faner mot commands/status/runner/widgets,
  ikke motsatt.
- Ingen sirkulære importer.
- Full test- og analysekontroll er kjørt.

### Brukerens oppgave

- Start programmet med den vanlige kommandoen, ikke ved å importere intern
  modul direkte.
- Klikk gjennom alle fire faner og avslutt normalt.
- Commit foreslått melding:

```text
Gjør launcher til et tynt inngangspunkt
```

## Trinn 10 – Avsluttende testopprydding og dokumentasjon

### Arbeid

- Kontroller at `tests/test_launcher.py` er fjernet eller redusert til tester
  av det offentlige inngangspunktet.
- Fordel testene etter modulene de tester.
- Erstatt gjenværende tester som leser Python-kildekode som tekst med
  oppførselstester der det er mulig.
- Oppdater denne planen med endelige filstørrelser og commit-hasher.
- Oppdater relevant utviklerdokumentasjon dersom modulstrukturen må forklares.

### Ferdigkriterium

- Hele pytest-samlingen består.
- Pyflakes består.
- Mypy har ingen nye feil sammenlignet med baseline.
- Windows-smoketest er utført eller registrert som et tydelig gjenstående
  punkt før neste versjon.
- Denne planen er markert ferdig.

### Brukerens oppgave

- Gjør en siste manuell gjennomgang på Windows 11 når du har tilgang til
  utviklingsmaskinen.
- Kontroller installasjon/oppdatering, browserstart, én ufarlig import,
  avbrytelse av en jobb og backup dry-run.
- Commit foreslått melding:

```text
Fullfør oppsplitting av launcher-tester
```

## Arbeidsflyt mellom hvert trinn

1. Brukeren ber om neste nummererte trinn.
2. Arbeidstreet kontrolleres. Urelaterte lokale endringer røres ikke.
3. Trinnet markeres `pågår` i denne filen.
4. Koden og testene endres bare innenfor trinnets omfang.
5. Fokuserte tester kjøres først, deretter hele kontrollsettet.
6. Resultat, eventuelle beslutninger og neste trinn skrives inn i denne filen.
7. Brukeren gjennomgår diffen og gjør commit.
8. Ved starten av neste trinn registreres forrige commit-hash i statustabellen.

Hvis en ny samtale eller en omstart skjer, skal arbeidet fortsette fra
statusfeltet og «Neste trinn» i denne filen, ikke rekonstrueres fra minnet.

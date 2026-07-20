# Launcher-arkitektur

Launcheren er delt i et tynt offentlig inngangspunkt, et appskall, fem faner
og fire fellesmoduler. Oppdelingen skal gjøre det mulig å endre og teste én
del uten å måtte kjenne hele Tk-applikasjonen.

## Moduler

| Modul | Ansvar |
|---|---|
| `launcher.py` | Stabilt offentlig `main()`-inngangspunkt. |
| `launcher_app.py` | Rotvindu, notebook, felles logg, busy-status og koordinering mellom fanene. |
| `launcher_main_tab.py` | Valg og oppretting av samling, server, oppdatering, oppretting og kontroll av snapshots og migrering. |
| `launcher_advanced_start_tab.py` | Normal, read-only, LAN-share og slideshow med port, filter og delay. Bruker serverprosessen som eies av hovedfanen. |
| `launcher_import_tab.py` | Import, rescan, check-source og unimport. |
| `launcher_tools_tab.py` | Scanner, statiske browsere, doctor, vacuum, filslettingsopprydding og eksport. |
| `launcher_setup_tab.py` | Status og installasjon for InsightFace, OpenCLIP og modellene deres. |
| `launcher_commands.py` | Rene byggere for argumentlistene som sendes til CLI og installasjonsscript. |
| `launcher_status.py` | Datamodeller og lesing av config-, Git-, migrerings-, samlings- og AI-status. |
| `launcher_runner.py` | Bakgrunnskjøring av subprocesser, avbrytelse og videresending av output. |
| `launcher_widgets.py` | Gjenbrukbare tooltips, valgdialoger og bekreftelsesdialoger. |

Avhengighetene skal peke fra `launcher` til `launcher_app`, og derfra til
fanene og fellesmodulene. Fellesmodulene skal ikke importere appen eller
fanene. Fanene får eksplisitte callbacks for blant annet kommandokjøring,
logging, statusoppdatering og dialoger. De skal ikke hente intern tilstand fra
`LauncherApp`.

Tk-operasjoner skal utføres på Tk-tråden. Arbeid som kan blokkere legges i en
bakgrunnstråd, og resultatet leveres tilbake gjennom callbacken `post_to_ui`.
`CommandRunner` eier subprocessen for én launcher-kommando om gangen, mens
serverprosessen eies av hovedfanen fordi den kan leve videre etter at knappen
som startet den er ferdig. Både hovedfanen og **Nettleser og deling** bruker den
samme startmekanismen.

Hovedfanen lagrer normaliserte oppstartsvalg for prosessen den eier: modus,
port og eventuelt slideshow-delay og filter. Samme valg åpner adressen til den
eksisterende serveren. Endrede valg krever bekreftet omstart; avvisning lar
prosessen fortsette, mens godkjenning stopper den kontrollert før ny prosess
startes. LAN-share og slideshow krever i tillegg den samme sikkerhetsadvarselen
før oppstart. Slideshowkommandoen inneholder bare `--slideshow`, `--delay` og
eventuelt `--filter`; CLI-en gjør selv modusen read-only, aktiverer previews og
binder den til LAN.

## Tester

`tests/test_launcher.py` tester bare det offentlige inngangspunktet. Appskall,
faner og fellesmoduler har tilsvarende `test_launcher_<modul>.py`-filer.
Atferdstester foretrekkes fremfor tester som leser Python-kildekode som tekst.
Noen få strukturelle tester av Tk-dialoger og faneoppbygging bruker fortsatt
kildekoden; de bør erstattes først når det finnes en liten og stabil GUI-
testdobbel som kan verifisere samme kontrakt.

Ved endringer i launcheren kjøres:

```powershell
python -m pytest -q
python -m ruff check bildebank tests
python -m pyflakes bildebank tests
python -m mypy bildebank
```

På Windows skal launcheren i tillegg startes via den vanlige kommandoen.
Kontroller at alle faner åpner, at minst én relevant handling kan startes, og
at vinduet og en eventuell serverprosess avsluttes normalt.

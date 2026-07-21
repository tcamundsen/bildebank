# Servermoduler

Denne oversikten beskriver ansvarsgrensene i den lokale HTTP-serveren. Den er
for utviklere og AI; brukerdokumentasjon hører hjemme i `docs/`.

## Inngang og avhengigheter

```text
server
  -> server_runtime
       -> server_handler
            -> server_endpoints_*
                 -> domene-, query- og HTML-moduler
```

- `server.py` er det stabile, offentlige inngangspunktet. Det re-eksporterer
  bare serverens offentlige API.
- `server_runtime.py` eier oppstart, sikker bind-validering,
  `BildebankServer`, servertilstand og browsernavigasjonscache.
- `server_handler.py` eier HTTP-livssyklus, read-only- og CSRF-kontroll,
  eksplisitt GET/POST-ruting samt generelle ressurser som filer, preview og
  dokumentasjon. Originalfiler og MP4-avspillingskopier strømmes her med
  støtte for én HTTP `Range` og `206 Partial Content`; stioppslag og
  range-parsing eies av `server_files.py`. Originalfiler og thumbnails slås
  bare opp via numerisk `file_id`; vilkårlige relative stier i samlingsmappen
  er ikke en del av serverens fil-API.
- `server_slideshow.py` bygger det faste slideshowutvalget og eier den minimale
  slideshow-siden. Når modusen er aktiv, bruker handleren en egen allowlist og
  slipper ikke forespørsler videre til de vanlige browserrutene.
- `server_endpoints_browser.py`, `server_endpoints_admin.py`,
  `server_endpoints_faces.py` og `server_endpoints_items.py` eier
  domenespesifikke HTTP-adaptere. De mottar handleren eksplisitt og skal ikke
  importere `server.py` ved runtime.

Ruting i handleren skal kalle endepunktfunksjonene direkte. Ikke legg tilbake
delegatmetoder på `BildebankRequestHandler` bare for å gjøre en enhetstest
enklere; testen skal importere funksjonen fra eiermodulen.

## Ved endringer

- Behold eksplisitt ruterekkefølge. Ikke innfør en generell router eller et
  web-rammeverk uten en egen beslutning.
- Patching i tester må gjøres der navnet slås opp, for eksempel
  `server_runtime.BildebankServer` for oppstart og den aktuelle
  `server_endpoints_*`-modulen for et endepunkt.
- Fil- og databaseendringer skal fortsatt eies av domenemoduler og holde
  target-låsen. HTTP-laget skal ikke implementere egen filflytting eller SQL.
- Etter endringer i handler eller runtime: kjør hele kontrollsettet og prøv
  serverstart mot en testsamling. Se også `server-oppsplitting.md`.

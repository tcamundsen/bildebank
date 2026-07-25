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
  Read-only-oppstart validerer hoveddatabasen gjennom en read-only-tilkobling.
  CLI-laget leser samtidig config uten legacy-migrering og hopper over
  oppdatering av lokal programstatus. Dette gjelder også LAN-share og
  slideshow. Den uttrykkelige LAN-share-profilen følger med som servertilstand
  og brukes av visningslaget til å utelate lokale kilde- og
  snapshot-repositorystier. Vanlig lokal read-only påvirkes ikke.
- Dashboard og maintenance-status bruker read-only-tilkoblinger uavhengig av
  servermodus. Dashboardets snapshotoversikt sikrer eller migrerer ikke lokal
  programdatabase. OpenCLIP- og InsightFace-status krever gjeldende eksplisitt
  schema og adopterer, migrerer eller oppdaterer ikke sidecar-databasene.
- Alle serverens lesehjelpere for InsightFace validerer eksisterende schema
  read-only. Person-, item- og filtersider oppretter eller migrerer derfor
  ikke face-databasen under GET. SQLite-vedlegg av face-databasen bruker
  eksplisitt `mode=ro`; en manglende database opprettes ikke som bivirkning.
- Handlerens delte browsertilkobling, browserqueries, filtersøk og
  sidecaroppslag bruker hoveddatabasens felles read-only-tilkobling. Den åpner
  SQLite med URI-støtte; dette er nødvendig for at `ATTACH` av face-databasen
  med `mode=ro` skal fungere på Windows. Serveroppstarten fullvaliderer
  schemaet og merker databasen som forberedt i prosessen; senere
  read-only-tilkoblinger gjør den billige versjonskontrollen.
- `server_handler.py` eier HTTP-livssyklus, read-only- og CSRF-kontroll,
  eksplisitt GET/POST-ruting samt generelle ressurser som filer, preview og
  dokumentasjon. Etter standardbibliotekets HTTP-parsing og før ruting
  validerer handleren én entydig `Host` og eventuell same-origin `Origin`
  gjennom `server_request.py`. Wildcard-binding godtar IP-litteraler, ikke
  vilkårlige domenenavn, slik at DNS-rebinding avvises.
  `BildebankRequestHandler.end_headers()` legger den felles, faste
  sikkerhetspakken fra `server_response.py` på alle ferdige responser,
  inkludert filer, redirects og tidlige HTTP-feil. Handlerens `Server`-header
  røper ikke Python-versjonen. Før CSRF-kontroll leser handleren POST-body
  gjennom den
  felles, tekstbaserte 1 MiB-grensen i `server_request.py`. Ugyldig eller
  duplisert `Content-Length`, ufullstendig eller ugyldig UTF-8 og enhver
  `Transfer-Encoding` avvises før endepunktet kjøres. Originalfiler og
  MP4-avspillingskopier strømmes her med
  støtte for én HTTP `Range` og `206 Partial Content`; stioppslag og
  range-parsing eies av `server_files.py`. Originalfiler og thumbnails slås
  bare opp via numerisk `file_id`; vilkårlige relative stier i samlingsmappen
  er ikke en del av serverens fil-API. I read-only-modus krever alle direkte
  medievarianter en aktiv `files`-rad. `server_files.py` validerer
  aktiv/slettet layout og databaseført stinøkkel, avviser symlinker og Windows
  reparse points i hele stien og åpner den kontrollerte filen med en stabil
  fil-deskriptor før responsheaderne sendes.
- GET `/search` viser bare skjemaet. OpenCLIP-søk, modell-preload og eksplisitt
  thumbnailtelling rutes som CSRF-beskyttet POST fordi de skriver til database
  eller kan utløse betydelig CPU-/filskanningsarbeid. Read-only
  vedlikeholdsstatus forblir GET.
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

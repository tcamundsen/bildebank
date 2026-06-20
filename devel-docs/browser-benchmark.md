# Browser benchmark

Dette er et opt-in utviklerverktøy for å måle hvor raskt bildebrowseren blar
mellom bilder i `run-server`.

Start først serveren mot bildesamlingen:

```bash
.venv/bin/python -m bildebank --target /path/to/bildesamling run-server --no-browser
```

Kjør deretter benchmarken fra en annen terminal. Bruk en konkret item-URL som
startpunkt:

```bash
.venv/bin/python tools/benchmark_browser.py --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10 --threshold-ms 250
```

`browser`-modus bruker Playwright og måler ekte klikk på `Neste bilde`.
Hvis Playwright ikke er installert, kan HTTP-modus brukes for å måle bare
serverresponsene. Den henter startsiden én gang først, og måler deretter hver
`Neste bilde`-navigasjon som én ny sidehenting:

```bash
.venv/bin/python tools/benchmark_browser.py --mode server --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10
```

`server-keepalive` bruker én HTTP-forbindelse og sender
`X-Bildebank-Benchmark: 1`. Da svarer serveren med opt-in `Server-Timing` for
faktisk `/item/...`-håndtering, og benchmarken skriver en `server:`-linje med
median for hvert serversteg:

```bash
.venv/bin/python tools/benchmark_browser.py --mode server-keepalive --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10
```

For å se hvor item-siden bruker tid internt, bruk profile-modus. Den går ikke
via HTTP, men bruker de samme Python-funksjonene som `run-server` bruker for en
item-side. Profile-modus støtter item-URL-er for hele samlingen, filtersøk og
importerte kilder, inkludert `/source/<id>/item/<id>`. Den kjører samme
databaseforberedelse som serveren før målingen starter:

```bash
.venv/bin/python tools/benchmark_browser.py --mode profile --target /path/to/bildesamling --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10
```

For en importert kilde:

```bash
.venv/bin/python tools/benchmark_browser.py --mode profile --target /path/to/bildesamling --url http://127.0.0.1:8765/source/7/item/736 --steps 100 --warmup 10
```

På Windows kan kommandoen for eksempel kjøres slik:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_browser.py --mode profile --target "C:\Users\Tom\Pictures\Bildebank" --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10
```

Skriv rådata til fil når tallene skal sammenlignes over tid:

```bash
.venv/bin/python tools/benchmark_browser.py --url http://127.0.0.1:8765/item/123 --json-output /tmp/bildebank-browser-benchmark.json
```

## Suite med flere start-URL-er

En suite er en JSON-liste med navn, start-URL og terskel for hver case:

```json
[
  {
    "name": "vanlig-bildevisning",
    "url": "http://127.0.0.1:8765/item/123",
    "threshold_ms": 10
  },
  {
    "name": "filtersok-video",
    "url": "http://127.0.0.1:8765/filter/type%3Avideo/item/123",
    "threshold_ms": 15
  }
]
```

Kjør hver case tre ganger:

```bash
.venv/bin/python tools/benchmark_browser.py --mode server-keepalive --suite benchmark-suite.json
```

`--repeat` styrer antall kjøringer per case. Samme `--mode`, `--steps`,
`--warmup` og `--timeout-ms` brukes for alle kjøringene. Beste kjøring velges
først etter færrest terskelbrudd, deretter lavest p95 og til slutt lavest
median.

En case er godkjent når beste kjøring har et antall terskelbrudd innenfor
`--min-failures` og `--max-failures`, inklusive grensene. Standard er 0 til 5.
Suite-rapporten viser terskel, beste median/p95/maks, terskelbrudd for beste
kjøring og terskelbrudd for hver repetisjon.

Bruk `--json-output` for å lagre alle kjøringer, valgt beste kjøring og samlet
pass/fail:

```bash
.venv/bin/python tools/benchmark_browser.py --mode server --suite benchmark-suite.json --repeat 5 --json-output suite-resultat.json
```

Exit code er `1` når `--threshold-ms` er satt og minst ett målt steg er tregere
enn terskelen. Det gjør scriptet egnet som manuell regresjonssjekk.

I suite-modus er exit code `1` når én eller flere cases feiler grensene. Ugyldig
suiteformat, konfigurasjonsfeil og runtime-feil gir exit code `2`.

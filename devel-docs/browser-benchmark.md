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
item-side. Profile-modus kjører samme databaseforberedelse som serveren før
målingen starter:

```bash
.venv/bin/python tools/benchmark_browser.py --mode profile --target /path/to/bildesamling --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10
```

På Windows kan kommandoen for eksempel kjøres slik:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_browser.py --mode profile --target "C:\Users\Tom\Pictures\Bildebank" --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10
```

Skriv rådata til fil når tallene skal sammenlignes over tid:

```bash
.venv/bin/python tools/benchmark_browser.py --url http://127.0.0.1:8765/item/123 --json-output /tmp/bildebank-browser-benchmark.json
```

Exit code er `1` når `--threshold-ms` er satt og minst ett målt steg er tregere
enn terskelen. Det gjør scriptet egnet som manuell regresjonssjekk.

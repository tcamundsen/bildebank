# Benchmark kjøres slik

Kjøres fra Windows, med laptop koblet til strøm, på min bildesamling.

```powershell
python tools/benchmark_browser.py --repeat 3 --mode server-keepalive --suite .\tools\bench-suite.json
python tools/benchmark_browser.py --repeat 3 --mode server-keepalive --suite .\tools\bench-suite-noopt.json


Fra tidligere bench.cmd script på windows.
.\.venv\scripts\python.exe tools\benchmark_browser.py --mode server-keepalive --url  "http://127.0.0.1:8765/filter/month%3A12%20day%3A24/item/1187" --steps 100 --warmup 10

.\.venv\scripts\python.exe tools\benchmark_browser.py --mode server-keepalive --url http://127.0.0.1:8765/person/Siril/no-faces/item/1257 --steps 100 --warmup 10

.\.venv\scripts\python.exe tools\benchmark_browser.py --mode server-keepalive --url http://127.0.0.1:8765/item/123 --steps 100 --warmup 10
```

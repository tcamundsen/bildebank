# Benchmark kjøres slik

Kjøres fra Windows, med laptop koblet til strøm, på min bildesamling.

```powershell
python tools/benchmark_browser.py --repeat 3 --mode server-keepalive --suite .\tools\bench-suite.json
python tools/benchmark_browser.py --repeat 3 --mode server-keepalive --suite .\tools\bench-suite-noopt.json
```

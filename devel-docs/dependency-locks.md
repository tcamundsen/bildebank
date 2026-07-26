# Oppdatere Python-låsene

Kjør bare når nye pakkeversjoner skal vurderes, i 64-bit Windows CPython 3.13:

```powershell
.\.venv\Scripts\python.exe .\tools\update_dependency_locks.py
```

Generatoren installerer ingenting. Inspiser de tre filene i `requirements`,
test basisinstallasjon, InsightFace og OpenCLIP på Windows, og commit dem bare
hvis alt fungerer. Låsene bruker pips innebygde SHA-256-kontroll.

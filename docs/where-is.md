# where-is
<!-- CLI-HELP-START -->
```text
usage: bildebank where-is [valg]

Vis hvor Bildebank og kjente bildesamlinger ligger

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`where-is` viser hvor Bildebank og kjente bildesamlinger ligger.
Kommandoen trenger ikke å kjøres fra en bildesamlingsmappe.

## Hva kommandoen viser

`where-is` skriver ut:

- hvor Bildebank-programmet ligger
- hvor Bildebank lagrer sin lille oversikt over kjente bildesamlinger
- hvilken mappe PowerShell står i akkurat nå
- hvilke bildesamlingsmapper Bildebank kjenner til

Eksempel:

```text
Bildebank-program:
  C:\Users\Tom\kode\bildebank

Programdata:
  C:\Users\Tom\kode\bildebank\.bildebank-program.sqlite3

Gjeldende mappe:
  C:\Users\Tom

Kjente bildesamlingsmapper:
  C:\Users\Tom\BildeSamling
    status: finnes
    sist brukt: 2026-05-07T21:15:00
```

## Når er dette nyttig?

Bruk `where-is` når du er usikker på hvor bildesamlingen ligger, eller når du
står i feil mappe i PowerShell.

Hvis Bildebank kjenner til en bildesamling, viser kommandoen også et forslag
til `cd`-kommando:

```powershell
cd "C:\Users\Tom\BildeSamling"
```

Kopier den linjen inn i PowerShell for å gå til bildesamlingen.

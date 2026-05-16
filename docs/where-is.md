# where-is

`where-is` viser hvor Bildebank og kjente bildesamlinger ligger.

## Referanse

```powershell
bildebank where-is
```

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

## Hvordan husker Bildebank bildesamlinger?

Når du oppretter en bildesamling med `create`, lagres den i en liten oversikt
ved siden av programmet.

Hvis du allerede har en bildesamling fra før, blir den lagt til automatisk
neste gang du bruker Bildebank fra den bildesamlingsmappen.


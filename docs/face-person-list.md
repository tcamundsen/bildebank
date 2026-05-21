# face-person-list

`face-person-list` lister personer i ansiktsdatabasen.

## Referanse

```powershell
bildebank face-person-list
```

Kommandoen viser en tabell med én linje per registrerte person.

Eksempel:

```text
Navn      Bilder  Ansikter  Forslag  Oppdatert
----      ------  --------  -------  -------------------
Kari          12        14       83  2026-05-09 12:34:56
```

Tallene betyr:

- `Bilder` er antall bilder der du selv har bekreftet at personen
  finnes. Dette teller bilder, ikke ansikter.
- `Ansikter` er antall ansikter du selv har koblet til personen med
  `face-person-add-face`.
- `Forslag` er antall ansikter `face-suggest` foreslår for personen. Dette er
  ikke bekreftet av deg.
- `Oppdatert` er tidspunktet personen sist ble oppdatert i ansiktsdatabasen.

Hvis samme person er bekreftet flere ganger i samme bilde, teller bildet bare
én gang i `Bilder`.

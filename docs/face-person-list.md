# face-person-list

`face-person-list` lister personer i ansiktsdatabasen.

## Referanse

```powershell
bildebank face-person-list
```

Kommandoen viser en linje per registrerte person.

Eksempel:

```text
Kari    bekreftede_bilder=12    bekreftede_ansikter=14    forslag=83    oppdatert=2026-05-09 12:34:56
```

Tallene betyr:

- `bekreftede_bilder` er antall bilder der du selv har bekreftet at personen
  finnes. Dette teller bilder, ikke ansikter.
- `bekreftede_ansikter` er antall ansikter du selv har koblet til personen med
  `face-person-add-face`.
- `forslag` er antall ansikter `face-suggest` foreslår for personen. Dette er
  ikke bekreftet av deg.
- `oppdatert` er tidspunktet personen sist ble oppdatert i ansiktsdatabasen.

Hvis samme person er bekreftet flere ganger i samme bilde, teller bildet bare
én gang i `bekreftede_bilder`.

# Database v9: manuell bildedato

Status: `schema_version=9` legger til manuell dato på `files`.

## Endring

`files` får tre nye kolonner:

- `manual_date_from TEXT`
- `manual_date_to TEXT`
- `manual_date_note TEXT`

Hvis begge datokolonner er `NULL`, brukes den opprinnelige `taken_date`.
Hvis begge er satt og like, er datoen eksakt. Hvis begge er satt og ulike,
tolkes datoen som et usikkert intervall.

Browserdatoen er midtpunktet mellom `manual_date_from` og `manual_date_to` når
manuell dato finnes. Filen flyttes ikke fysisk når manuell dato settes.

## Indekser

Ytelsesindeksene som sorterer browseren bygges på nytt, fordi sorteringen nå
bruker manuell browserdato når den finnes.

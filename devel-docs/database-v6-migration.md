# Database v6: eksplisitte ytelsesindekser

Status: `schema_version=6` gjør ytelsesindeksene til en eksplisitt del av
databaseskjemaet.

## Bakgrunn

Tidligere ble flere `CREATE INDEX IF NOT EXISTS` kjørt via vanlig
databaseåpning. Det var praktisk mens funksjoner ble lagt til, men det gjorde
`run-server` dyrere enn nødvendig, særlig når `.bilder.sqlite3` ligger på et
tregt filsystem.

## Regler etter v6

- Nye databaser opprettes direkte med alle ytelsesindekser.
- `bildebank migrate` oppgraderer v5-databaser til v6 ved å opprette manglende
  ytelsesindekser.
- `schema_version=6` settes først etter at indeksene er opprettet og databasen
  er validert.
- Vanlig `db.connect()` skal ikke kjøre opportunistisk schema- eller
  indeksoppretting.
- `run-server` forbereder databasen én gang ved oppstart. Etterpå kan
  requestene bruke lettere databaseåpninger i samme prosess.

## Integritet

Indeksene er ytelsesstruktur. Dataintegritet ivaretas fortsatt av tabeller,
constraints, foreign keys, migreringsrekkefølge, `foreign_key_check` og
`integrity_check`.

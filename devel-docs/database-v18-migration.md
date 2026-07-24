# Database v18: kortlivet filflyttingsjournal

Status: `schema_version=18` rydder terminale rader fra
`pending_file_moves`. Tabellstrukturen er uendret.

## Kontrakt

`pending_file_moves` er intern arbeidstilstand som bare Bildebank bruker for
trygg recovery av filflytting. Tabellen er ikke historikk og skal bare
inneholde uavklarte rader:

- `state='prepared'`
- `completed_at IS NULL`
- valgfri `last_error` når automatisk recovery ikke kan avgjøre tilstanden

Etter en vellykket flytting slettes journalraden i samme transaksjon som
`files` oppdateres. Hvis recovery entydig fastslår at flyttingen aldri skjedde,
slettes journalraden i recovery-commiten uten å endre `files`.

Krasj før commit ruller slettingen tilbake. Den uavklarte `prepared`-raden
blir dermed liggende og kan behandles ved neste recovery.

## Éngangsopprydding

Migrering fra v17 til v18 sletter eksisterende rader med
`state='completed'` eller `state='aborted'`. Uavklarte `prepared`-rader og
deres `last_error` beholdes.

En database som allerede er migrert til v17, kjører ikke
sidecar-oppryddingen på nytt og får ingen nye InsightFace-backuper ved
v18-migreringen. Migrering fra v16 eller eldre direkte til v18 utfører både
v17-sidecaroppryddingen og v18-journaloppryddingen.

## Tester

Regresjonstestene dekker:

- sletting av journalraden etter vanlig `remove` og `undelete`
- sletting etter fullført eller entydig avbrutt recovery
- sletting etter flyttende `refresh-metadata` og snapshot-recovery
- bevaring av uavklarte `prepared`-rader med `last_error`
- éngangsopprydding av eldre `completed`- og `aborted`-rader
- v17 til v18 uten gjentatt sidecar-opprydding eller InsightFace-backup

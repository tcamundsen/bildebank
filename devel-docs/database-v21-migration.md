# Database v21: tombstones og purge-journal

Status: Implementert. Skjemaet brukes av den ferdige purge-flyten som er
beskrevet i `devel-docs/sletting-med-toombstone.md`.

## Formål

V21 innfører databasestrukturen som trengs for den bekreftede, permanente
sletteflyten:

- `file_tombstones` lagrer SHA-256 og visningsinformasjon etter at en fil
  senere er permanent slettet
- `pending_file_purges` er en restriktiv journal som beskytter `files`-raden
  mens en bekreftet purge pågår
- fire SQLite-triggere håndhever at samme SHA-256 ikke kan finnes i både
  `files` og `file_tombstones`

Selve migreringen aktiverer eller starter ingen fysisk permanent sletting.
Den ferdige brukerflyten krever fortsatt en separat, eksplisitt bekreftelse på
`/settings/removed`.

## Migrering fra v20

Migreringen kjører i én SQLite-transaksjon og:

1. oppretter de to tomme tabellene
2. oppretter visningsindeksen på `file_tombstones(purged_at, id)`
3. oppretter den restriktive fremmednøkkelen fra
   `pending_file_purges.file_id` til `files.id`
4. oppretter krysstabell-triggerne
5. setter `schema_version=21` og logger migreringen

Migreringen skanner eller endrer ikke mediefiler. Den endrer heller ingen
eksisterende `files`- eller `file_sources`-rader, og den oppretter ikke
tombstones fra `deleted/` eller fra manglende filer.

## Slutt-transaksjonen

Databaselaget har en atomisk overgang for en allerede journalført purge.
Funksjonen laster og validerer identiteten, fjerner purge-posten og
`file_sources`, fjerner `files`-raden og oppretter tombstonen i samme
transaksjon. Den restriktive fremmednøkkelen gjør at andre kodeveier ikke kan
fjerne `files`-raden mens journalen finnes.

Hvis tombstone-innsettingen feiler, rulles hele overgangen tilbake. Både
purge-posten, `files`-raden og proveniensen blir da stående.

## Tester

Regresjonstestene dekker:

- ny v21-database og v20 til v21
- read-only `migrate --check`
- uendrede mediefiler, `files` og `file_sources`
- tabeller, indeks, restriktiv fremmednøkkel og triggere
- `INSERT` og endring av SHA-256 i begge retninger
- blokkert sletting av en journalført `files`-rad
- vellykket slutt-transaksjon og full rollback ved feil

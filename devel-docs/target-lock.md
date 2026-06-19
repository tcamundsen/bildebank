# Target-lås

`TargetLock` bruker `.bildebank.lock` i roten av bildesamlingen for å hindre at
operasjoner som ikke tåler samtidig kjøring endrer samme samling parallelt.

## Invariant for filflytting

En operasjon som flytter en samlingsfil og oppdaterer hoveddatabasen, skal ta
target-låsen før databaseoppslag og validering. Låsen skal holdes under
filflyttingen, gjennom databaseoppdateringen og til etter commit. Låsen skal
fjernes igjen både ved suksess og feil.

## Beskyttede operasjoner

- `import` og `rescan-source` holder target-låsen mens filer kopieres og
  importdatabasen oppdateres.
- `unimport` holder target-låsen under validering, filendringer og
  databaseoppdatering.
- `remove` og `undelete` bruker den felles modulen `file_lifecycle.py` fra både
  CLI og web. Modulen tar låsen før oppslag og holder den til etter commit.
- `backup` låser kildesamlingen mens backup-speilet oppdateres.
- `migrate` holder låsen mens hoveddatabasen migreres.
- `make-thumbnails` holder låsen mens thumbnail-settet oppdateres.

## Ulåste skriv som må vurderes senere

Følgende skriv er ikke endret i arbeidet med `remove` og `undelete`:

- oppfrisking og lagring av mediemetadata i hoveddatabasen;
- oppretting og fjerning av tagger på filer;
- GPS-skanning, manuell GPS/H3-plassering og geografiske hjelpetabeller;
- ansiktsskanning, personer, koblinger og forslag i ansiktsdatabasen;
- bildeskanning og embeddings i bildesøkdatabasen;
- manuell dato og visningsrotasjon fra webgrensesnittet.

Disse operasjonene må auditeres separat. Det må avgjøres hvilke som trenger
target-lås, hvilke som kan bruke en egen lås per sidedatabase, og hvilke som er
trygge med bare SQLite-transaksjoner. Denne oversikten gir ikke i seg selv
garanti mot samtidig skriving for disse operasjonene.

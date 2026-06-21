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
- `tag-add`, `tag-remove` og tilsvarende weboperasjoner holder låsen fra før
  filoppslag og validering til etter at taggendringen er committed.
- `geo-scan` holder låsen fra før første databaseoppslag til siste batch er
  committed.
- setting og fjerning av manuell H3-lokasjon holder låsen fra før filoppslag
  og validering til etter commit.
- `face-scan`, `face-suggest`, `face-reset` og alle endringer av personer,
  ansiktskoblinger og manuelle person-i-bilde-koblinger holder låsen gjennom
  hele operasjonen. Den sammensatte weboperasjonen som oppretter en person og
  kobler et ansikt bruker én lås og én transaksjon.
- `date-set`, `date-clear` og tilsvarende weboperasjoner for manuell dato
  holder låsen fra før filoppslag og validering til etter commit.
- visningsrotasjon fra web holder låsen gjennom oppslag, beregning av ny
  rotasjon og commit.
- `image-scan` holder låsen mens aktive bilder velges, bildefilene leses og
  embeddings lagres.
- `image-search` fra CLI og web holder låsen mens embeddings leses og
  søkeresultater lagres. CLI holder den også til `image-search.html` er skrevet.
- oppretting, endring og sletting av brukertaggdefinisjoner holder låsen fra
  før databaseoppslag og validering til etter commit.
- lagring og sletting av H3-cellenavn og egendefinerte steder holder låsen fra
  før første databaseoppslag til etter commit.

## Ulåste skriv som må vurderes senere

Følgende skriv bruker fortsatt ikke target-låsen:

- oppfrisking og lagring av mediemetadata i hoveddatabasen;

Denne operasjonen må auditeres separat. Det må avgjøres om den trenger
target-lås eller er trygg med bare SQLite-transaksjoner. Denne oversikten gir
ikke i seg selv garanti mot samtidig skriving for operasjonen.

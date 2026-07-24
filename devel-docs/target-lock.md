# Target-lås

`TargetLock` bruker `.bildebank.lock` i roten av bildesamlingen for å hindre at
operasjoner som ikke tåler samtidig kjøring endrer samme samling parallelt.

Låsfilen opprettes eksklusivt med private filrettigheter og får en unik
eier-ID. Opprettelsen rydder opp også hvis den avbrytes før context manageren
er ferdig etablert. Ved frigjøring kontrolleres både filidentitet og innhold,
slik at en eldre prosess aldri fjerner en låsfil som har blitt erstattet av en
nyere lås. Uventede, store eller lenkede låsfiler behandles som opptatte uten
at innholdet leses eller vises.

## Invariant for filflytting

En operasjon som flytter en samlingsfil og oppdaterer hoveddatabasen, skal ta
target-låsen før databaseoppslag og validering. Låsen skal holdes under
filflyttingen, gjennom databaseoppdateringen og til etter commit. Låsen skal
fjernes igjen både ved suksess og feil.

Fra schema v12 skal slike operasjoner først skrive en `pending_file_moves`-rad
og committe den før fysisk flytting. Oppstartsrecovery av `pending_file_moves`
tar også target-låsen før den eventuelt fullfører eller aborterer en entydig
flytting.

Den fysiske flyttingen skal være no-clobber: en målsti som finnes eller dukker
opp under operasjonen skal aldri overskrives. `remove`, `undelete` og
`refresh-metadata` bruker den felles flytteprimitiven i `safe_file_move.py`.

## Beskyttede operasjoner

- `import` og `rescan-source` holder target-låsen mens filer kopieres og
  importdatabasen oppdateres.
- `unimport` holder target-låsen under validering, filendringer og
  databaseoppdatering.
- `remove` og `undelete` bruker den felles modulen `file_lifecycle.py` fra både
  CLI og web. Modulen tar låsen før oppslag og holder den til etter commit.
  Før en remove-flytting valideres og ATTACH-es eksisterende OpenCLIP- og
  InsightFace-databaser. Etter flyttingen slettes bildeavhengige sidecar-rader
  i samme commit som slettemarkeringen og fullføringen av flyttejournalen.
  Recovery utfører den samme idempotente oppryddingen.
  Hvis en vanlig exception oppstår etter en fysisk webflytting, kjører
  webhandlingen recovery før den svarer og behandler operasjonen som vellykket
  når ønsket database- og filtilstand ble fullført entydig.
- `migrate` holder låsen mens hoveddatabasen migreres.
- `make-thumbnails` holder låsen mens thumbnail-settet oppdateres.
- `make-video-previews` holder låsen mens MP4-cachefiler kontrolleres og
  erstattes. `--dry-run` skriver ikke og tar derfor ikke låsen.
- `make-browser`, `make-person-browser` og
  `make-people-browser` holder låsen mens de leser
  databasegrunnlaget og skriver HTML-filer.
- `tag-add`, `tag-remove` og tilsvarende weboperasjoner holder låsen fra før
  filoppslag og validering til etter at taggendringen er committed.
- `geo-scan` holder låsen fra før første databaseoppslag til siste batch er
  committed.
- setting og fjerning av manuell H3-lokasjon holder låsen fra før filoppslag
  og validering til etter commit.
- `face-scan` holder låsen mens den velger hvilke aktive filer som skal
  scannes, og kort for hver lagring i ansiktsdatabasen. InsightFace-kjøringen
  mellom disse periodene skjer uten target-lås. Før resultatet lagres,
  kontrolleres det at filen fortsatt er aktiv og har samme SHA-256. Resultatet
  forkastes hvis filen er endret eller fjernet i mellomtiden. En intern
  skanneidentitet gjør også at en eldre scan ikke kan skrive resultater etter
  `face-reset --all` eller etter at en ny scan er startet.
- `face-suggest`, `face-reset` og alle endringer av personer, ansiktskoblinger
  og manuelle person-i-bilde-koblinger holder låsen gjennom hele operasjonen.
  Den sammensatte weboperasjonen som oppretter en person og kobler et ansikt
  bruker én lås og én transaksjon.
- `date-set`, `date-clear` og tilsvarende weboperasjoner for manuell dato
  holder låsen fra før filoppslag og validering til etter commit.
- visningsrotasjon fra web holder låsen gjennom oppslag, beregning av ny
  rotasjon og commit.
- lagring og fjerning av kommentarer fra web holder låsen gjennom filoppslag
  og commit.
- `image-scan` holder låsen mens aktive bilder velges, og kort for hver
  embedding som lagres. OpenCLIP-kjøringen mellom disse periodene skjer uten
  target-lås. Før lagring kontrolleres det at filen fortsatt er aktiv og har
  samme SHA-256. En intern skanneidentitet hindrer at en eldre scan skriver
  etter at en ny scan er startet.
- `image-search` fra CLI og web holder låsen mens embeddings leses og
  søkeresultater lagres. CLI holder den også til `image-search.html` er skrevet.
- oppretting, endring og sletting av brukertaggdefinisjoner holder låsen fra
  før databaseoppslag og validering til etter commit.
- lagring og sletting av H3-cellenavn og egendefinerte steder holder låsen fra
  før første databaseoppslag til etter commit.
- `refresh-metadata` holder låsen fra før filer og database leses til siste
  databaseendring er committed. Låsen beholdes over del-commits og
  filflyttinger. En fil som skal flyttes verifiseres mot databaseført SHA-256
  før pending-raden opprettes. `--dry-run` skriver ikke og tar derfor ikke
  låsen.
- `snapshot create` kjører entydig pending-move-recovery under sin egen
  target-lås før inventaret bygges. Manglende eller skadet hoveddatabase
  muteres ikke, slik at recovery-snapshot fortsatt kan opprettes.
- lazy lagring av avledet mediemetadata (bredde, høyde og orientering) tar
  låsen ved cache-miss før filen leses og holder den til cacheoppdateringen er
  committed. Cache-hit er skrivefri og tar ikke låsen.

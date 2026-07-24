# Integritetskontrakt for doctor og check-source

Dette dokumentet beskriver hva Bildebank mener med en frisk bildesamling.
Kontrakten skal brukes av `doctor`, `check-source`, migreringsvalidering og
eventuelle senere, eksplisitte reparasjonskommandoer.

## Sikkerhetsgrense

- `doctor` og `doctor --deep` er read-only. De skal ikke endre config,
  hoveddatabase, sidecar-databaser, mediefiler eller lokal programstatus.
- SQLite-databaser åpnes med `mode=ro` og `PRAGMA query_only=ON`.
- Doctor rapporterer pending-køer, men kjører ikke recovery eller opprydding.
- En eventuell target-lås er bare koordinering for et konsistent øyeblikksbilde.
  Den skal ikke utløse recovery.
- Doctor foreslår undersøkelse og sikkerhetskopi før databaseendringer. Den
  reparerer ikke manglende filer, feil innhold, orphan-filer eller
  provenienskonflikter.

## Hoveddatabase

En frisk hoveddatabase:

- bruker gjeldende schema og forventet intern struktur
- består `PRAGMA integrity_check`
- har ingen rader fra `PRAGMA foreign_key_check`
- har bare uavklarte `prepared`-rader i `pending_file_moves`

En database som ikke består den strukturelle integritetskontrollen skal ikke
brukes som grunnlag for fil- eller proveniensdommer i samme doctor-kjøring.

## `files` og `file_sources`

- Hver `files`-rad, også en slettet rad, har minst én `file_sources`-rad.
- Hver `file_sources`-rad peker på en eksisterende `files`-rad og en
  eksisterende `sources`-rad.
- `file_sources.sha256` er lik `files.sha256`.
- `file_sources.size_bytes` er lik `files.size_bytes`.
- En aktiv fil og en slettet fil beholder proveniens så lenge `files`-raden
  finnes.

## Databaseførte samlingsstier

- `files.target_path` er relativ, inneholder ikke `..` og peker innenfor
  samlingsroten.
- Ingen sti går gjennom symlinker eller Windows reparse points.
- `files.target_path_key` stemmer med normalisert nøkkel for `target_path`.
- En aktiv fil ligger under `udatert/` eller en gyldig års-/månedsmappe.
- En aktiv fil har ikke `deleted_original_target_path`.
- En slettet fil ligger under `deleted/` og har
  `deleted_original_target_path`.
- En slettet fils sti under `deleted/` stemmer med
  `deleted_original_target_path`.
- En fil på databaseført målsti er en vanlig fil, ikke en symlink, junction
  eller annen reparse point.

Stivalideringen skjer før filtilgang. Hvis en databaseført sti ikke består
kontrakten, hopper doctor over filkontrollene i den kjøringen. Eksisterende
stikomponenter kontrolleres med `follow_symlinks=False`; stivalideringen skal
ikke bruke `resolve()` på den databaseførte filstien.

## Filer på disk

Vanlig doctor kontrollerer for både aktive og slettede filer:

- at filen finnes på databaseført sti
- at filen er en vanlig fil uten lenker
- at filstørrelsen stemmer med `files.size_bytes`

`doctor --deep` kontrollerer i tillegg SHA-256 for både aktive og slettede
filer. En fil som endres mens den leses skal rapporteres som uavklart, ikke som
verifisert.

Orphan-medier er støttede mediefiler under administrerte samlingsmapper som
ikke har noen `files`-rad. De rapporteres, men adopteres eller slettes ikke.

## Pending-køer

- En rad i `pending_file_moves` er `FEIL`, fordi filtilstanden er uavklart.
- En vanlig, fullt identifisert rad i `pending_file_deletes` er `OBS`, fordi en
  ekstra fil fortsatt finnes.
- En delete-rad med usikker sti, manglende forventet SHA-256/størrelse eller
  endret fil er `FEIL`.
- En fullt identifisert delete-rad der filen allerede mangler er `OBS`; raden
  blir stående slik at en eksplisitt cleanup kan avklare køtilstanden.
- En delete-rad som fortsatt peker på en `files`-rad er `FEIL`.
- Eksisterende pending-delete-filer hashes via et åpent filhåndtak. Filtype,
  identitet, størrelse og endringstid kontrolleres før og etter lesing. En fil
  som byttes eller endres under kontrollen blir ikke godkjent.
- Ingen pending-rad behandles automatisk av doctor.

## Sidecar-databaser

- Eksisterende OpenCLIP- og InsightFace-databaser åpnes read-only.
- Doctor migrerer, adopterer eller oppretter ikke sidecar-databaser.
- Alle InsightFace-modelldatabaser under samlingens konfigurerte
  databasemappe skal kontrolleres, ikke bare aktiv modell.
- Sidecar-rader som peker på manglende eller slettede `files`-rader er feil.
- Kopiert `target_path`, `target_path_key` og SHA-256 skal stemme med
  hoveddatabasen der sidecar-schemaet lagrer disse verdiene.

## Kontrollnivåer

Vanlig doctor kjører schema-, databasehelse-, proveniens-, sti-, pending-,
eksistens- og størrelseskontroller. Store sidecar-databaser kan bruke
`quick_check` dersom full kontroll blir for treg.

`doctor --deep` kjører de samme kontrollene og legger til full SHA-256 av
aktive og slettede mediefiler samt full integritetskontroll av sidecar-
databaser.

## Implementeringsstatus

Ferdig:

- read-only-kontrakten for vanlig og dyp doctor
- uttrykkelig `integrity_check` og `foreign_key_check` for hoveddatabasen
- uttrykkelig read-only gate for gjeldende databaseschema
- alle `files` ↔ `file_sources`-invarianter, også for slettede filer
- full databaseført stivalidering for `files`, inkludert relativt format,
  aktiv/slettet mappeplassering, `deleted_original_target_path`,
  `target_path_key`, symlinker og Windows reparse points
- eksistens, vanlig filtype uten lenker og `size_bytes` for både aktive og
  slettede databasefiler
- rapportering av `pending_file_moves`
- read-only kontroll av `pending_file_deletes`, inkludert sti, referanser,
  forventet innholdsidentitet og stabil SHA-256
- eksisterende kontroller for aktive filer og OpenCLIP-orphans

Gjenstår:

- dyp SHA-256 for slettede filer
- alle InsightFace-modeller og full sidecar-konsistens
- samme read-only hoveddatabase- og stiregler i `check-source`

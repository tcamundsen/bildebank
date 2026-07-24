# Database v17: opprydding av bildedata i sidecar-databaser

Status: `schema_version=17` innfører en éngangsopprydding av gamle
OpenCLIP- og InsightFace-rader. Hoveddatabasens tabellstruktur er ellers
uendret fra v16.

## Hvorfor hovedskjemaet økes

Eldre versjoner lot sidecar-data bli liggende når et bilde ble flyttet til
`deleted/`, eller når siste importreferanse og dermed `files`-raden ble
fjernet. Om en rad er foreldet avgjøres av hoveddatabasen:

- en `files`-rad med `deleted_at IS NULL` er aktiv og beholder sidecar-data
- en `files`-rad med `deleted_at IS NOT NULL` er eksplisitt slettet og ryddes
- en sidecar-`file_id` som ikke finnes i `files`, ryddes som en rest etter
  blant annet eldre `unimport`

Oppryddingen styres derfor av hovedskjemaets migrering, ikke av en ny
OpenCLIP- eller InsightFace-schema-versjon.

Kriteriet er bevisst konservativt: enhver aktiv `files`-rad beholdes. I en
normal samling betyr det at bildet fortsatt har minst én `file_sources`-rad.
En slettet `files`-rad ryddes selv om den fortsatt har kildehenvisninger,
fordi `remove` og sletting i webgrensesnittet er en eksplisitt beslutning om
at bildet ikke skal brukes.

## Databaser som omfattes

Migreringen bruker samme oppdagelse som ordinær sletting og `unimport`:

- legacy-databasen `.bilder-faces.sqlite3`, hvis den finnes
- alle `*.sqlite3` i konfigurert `face_recognition.database_dir`
- `.bilder-openclip.sqlite3`, hvis den finnes

Manglende sidecar-databaser opprettes ikke. InsightFace-modeller lastes ikke.
Eksisterende sidecar-schema valideres før opprydding.

## InsightFace-opprydding

For slettede eller manglende `file_id` fjernes:

- `scanned_files`
- `faces`
- koblinger i `person_faces`
- koblinger i `person_files`
- `face_suggestions` der enten `face_id` eller `reference_face_id` peker på
  et ansikt som fjernes

`persons` beholdes. Aktive bilder, deres ansikter, bekreftelser og forslag
beholdes.

## OpenCLIP-opprydding

For slettede eller manglende `file_id` fjernes:

- `image_embeddings`
- `image_search_results`
- `image_search_runs` som hadde foreldede resultater og ikke har noen aktive
  resultater

Eksisterende tomme søkekjøringer berøres ikke. En søkekjøring som også har
aktive resultater beholdes, men de foreldede resultatene fjernes.

## Backup og transaksjon

CLI-en lager som før backup av `.bilder.sqlite3` før migreringen.

Før oppryddingen valideres hver eksisterende InsightFace-database og kopieres
med SQLite backup-API-et. Backupen integritetskontrolleres og får navn etter
dette mønsteret:

```text
antelopev2.sqlite3.backup-before-main-schema-17-<tid>-<uuid>
```

OpenCLIP-data er regenererbare og får ikke en egen migreringsbackup.

Sidecar-databasene kobles til hovedforbindelsen med `ATTACH`. Hovedendringen og
alle sidecar-slettingene kjøres i samme transaksjon. Etter oppryddingen kjøres
`foreign_key_check` og `integrity_check` på hver tilkoblet sidecar-database.
Ved feil rulles hoveddatabasen og alle sidecar-endringer tilbake, og
`schema_version` forblir v16. InsightFace-backupene beholdes.

`bildebank migrate --check` viser oppryddingen i planen, men åpner ikke
sidecar-databasene for skriving og lager ingen backuper.

## Fremtidig permanent tømming av deleted

V17 endrer ikke kontrakten for mediefiler og sletter ingen bildefiler. Dersom
det senere innføres en eksplisitt funksjon for permanent tømming av
`deleted/`, er OpenCLIP- og InsightFace-data allerede fjernet da bildet først
ble slettet. En slik funksjon må fortsatt få en egen sikkerhetsvurdering.

## Tester

Regresjonstestene dekker:

- bevaring av et aktivt bilde som fortsatt har en importreferanse
- opprydding av et slettet bilde selv om kildehenvisningen fortsatt finnes
- opprydding av sidecar-rader uten tilsvarende `files`-rad
- legacy-InsightFace og flere modelldatabaser
- backup av hver InsightFace-database før sletting
- bevaring av `persons`
- OpenCLIP-søk med aktive, foreldede og tomme kjøringer
- rollback av hoveddatabase, OpenCLIP og InsightFace ved en sen feil

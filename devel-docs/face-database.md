# Face-database

Dette dokumentet beskriver den separate face-databasen `.bilder-faces.sqlite3`.
Den vanlige bildesamlingsdatabasen er `.bilder.sqlite3` og har egen
schema-versjon i `bildebank/db.py`.

Face-databasen brukes for data som kan gjenskapes fra bildene, men som kan være
tidkrevende å bygge på nytt:

- resultat fra `face-scan`
- registrerte personer
- bekreftede ansiktskoblinger
- forslag fra `face-suggest`

## Dagens versjon

Dagens face-schema er `FACE_SCHEMA_VERSION = 5` i `bildebank/face.py`.

Face-schema-versjonen lagres i:

```text
meta.schema_version
```

Nye face-databaser opprettes direkte som v5.

## Tabeller i v5

V5 inneholder disse hovedtabellene:

- `meta`
- `scanned_files`
- `faces`
- `persons`
- `person_faces`
- `person_files`
- `face_suggestions`

`scanned_files` lagrer én rad per scannet bildefil.
`faces` lagrer ansiktsbokser og embeddings.
`persons` lagrer personnavn.
`person_faces` lagrer bekreftede koblinger mellom person og face-id.
`person_files` lagrer manuelle bekreftelser på at en person er i en fil,
uten å bekrefte et bestemt face-id.
`face_suggestions` lagrer beregnede forslag. `reference_face_id` peker på det
bekreftede ansiktet som ga høyest similarity for forslaget.

`person_files` har disse kolonnene:

```sql
person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE
file_id INTEGER NOT NULL
confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
PRIMARY KEY(person_id, file_id)
```

Tabellen har indeks på `file_id`.

Manuelle person-fil-koblinger er visningsdata. De skal ikke brukes som
treningsgrunnlag for `face-suggest`, og de skal ikke vises i “Bekreftede
bilder”, fordi den visningen brukes som grunnlag for forslag.

## Stier

Stier til filer inne i bildesamlingen skal lagres relativt til aktiv
samlingsrot.

Eksempel:

```text
2021/08/2019-1-6-1.jpg
```

De skal ikke lagres som absolutte Windows- eller Linux-stier.

Runtime-kode som trenger en faktisk filsti skal gjøre:

```text
target_root / relative_path
```

Dette er nødvendig for at en bildesamling skal kunne flyttes eller få nytt
mappenavn uten at face-databasen slutter å virke.

## Path-forutsetning

`connect_face_db(target)` gjør dette:

- åpner `.bilder-faces.sqlite3`
- sørger for gjeldende face-schema
- oppdaterer `meta.target_path` til aktiv samlingsrot

Face-databasen skal allerede lagre samlingsinterne bildestier relativt. Koden
skal ikke forsøke å reparere gamle absolutte `target_path`-verdier ved åpning av
databasen. Slike databaser må migreres eller regenereres før de brukes med
gjeldende kode.

## Migrering v2 til v3, v3 til v4 og v4 til v5

V2 hadde tabeller for gruppeflyten:

- `face_group_runs`
- `face_groups`
- `face_group_members`

Gruppeflyten er fjernet fordi den ga for dårlig datakvalitet. Brukeren skal i
stedet bekrefte gode enkeltansikter og deretter bruke `face-suggest`.

V3 fjerner gruppetabellene.

Produksjonsåpningene går gjennom `prepare_face_schema()`:

- les `meta.schema_version`
- ta en konsistent SQLite-backup før en kjent v2-, v3- eller v4-database endres

Selve migreringen skjer deretter i `apply_face_schema()`:

- start `BEGIN IMMEDIATE` og les versjonen på nytt etter at skrivelåsen er tatt
- hvis versjonen er 0, opprett v5 direkte
- hvis versjonen er 2, kjør `migrate_face_schema_v2_to_v3()`
- hvis versjonen er 3, kjør `migrate_face_schema_v3_to_v4()`
- hvis versjonen er 4, kjør `migrate_face_schema_v4_to_v5()`
- sett `meta.schema_version = 5`
- valider gjeldende face-schema
- commit hele migreringen samlet, eller rull alt tilbake ved feil og avbrudd

Backupen legges ved siden av face-databasen og får et unikt navn på formen:

```text
buffalo_l.sqlite3.backup-before-schema-5-20260724-120000-<unik-id>
```

Backupen tas med SQLite sitt backup-API, slik at også committed innhold i en
eventuell WAL blir med. Hvis backup eller integritetskontrollen av backupen
feiler, skal migreringen ikke starte. Face-migreringsbackuper under
bildesamlingen tas med i snapshots som migreringsbackuper.

`migrate_face_schema_v2_to_v3()` gjør bare dette:

```sql
DROP TABLE IF EXISTS face_group_members;
DROP TABLE IF EXISTS face_groups;
DROP TABLE IF EXISTS face_group_runs;
```

Dette sletter ikke bilder, scannede ansikter, personer, bekreftelser eller
forslag.

`migrate_face_schema_v3_to_v4()` oppretter bare `person_files` og indeksen på
`file_id`. Den endrer ikke eksisterende personer, ansiktskoblinger eller
forslag.

`migrate_face_schema_v4_to_v5()` legger til `reference_face_id` i
`face_suggestions` og en indeks på kolonnen. Eksisterende forslag beholder
verdien `NULL` frem til `face-suggest` kjøres på nytt.

V4→v5-steget er idempotent for den kjente mellomtilstanden fra eldre kode:
Hvis `reference_face_id` allerede finnes, opprettes bare eventuell manglende
indeks før migreringen fullføres. Dette gjør at en v4-database som ble delvis
endret av et tidligere avbrudd, kan åpnes og migreres på nytt uten at personer,
bekreftelser eller forslag går tapt.

## Validering

Etter schema-oppretting eller migrering kjører `validate_current_face_schema()`.

For v5 skal:

- alle forventede tabeller og nødvendige kolonner finnes
- legacy-gruppetabellene ikke finnes
- samlingsinterne stier være relative
- `PRAGMA foreign_key_check` ikke finne brutte deklarerte referanser

Hvis en database sier `schema_version=5`, men ikke oppfyller disse kravene,
skal programmet feile tydelig i stedet for å opprette, bygge om eller slette
tabeller lydløst.

Produksjonstilkoblinger aktiverer også `PRAGMA foreign_keys = ON`, slik at nye
skriveoperasjoner ikke kan opprette brutte deklarerte referanser. Eldre
støttede schema-varianter bygges ikke om bare for å legge til flere foreign
keys; valideringen respekterer derfor de deklarerte relasjonene i den aktuelle
databasen.

Etter at en ny database er opprettet eller en eldre database er migrert, kjøres
i tillegg både `PRAGMA foreign_key_check` og `PRAGMA integrity_check` før
transaksjonen committes. En feil ruller tilbake hele opprettingen eller
migreringen. Full `integrity_check` kjøres ikke ved hver åpning av en allerede
gjeldende database, fordi face-databasen kan inneholde mange store embeddings.

Dette følger samme prinsipp som hoveddatabasen:

- eksplisitt migrering fra kjent eldre versjon
- ingen støtte for ukjent eldre layout
- avvis nyere schema-versjon enn programmet støtter
- ikke reparer en inkonsistent gjeldende versjon ved å slette data i stillhet

## Forskjell fra hoveddatabasen

Face-databasen migreres ikke av `bildebank migrate`.

Den migreres når face-kode åpner `.bilder-faces.sqlite3` med
`connect_face_db()`. Migreringen er beskyttet av egen backup og én
SQLite-transaksjon. Samtidige åpninger leser schema-versjonen på nytt etter
skrivelåsen, slik at bare den første åpningen utfører migreringen.

Likevel skal face-migreringer være små, versjonsstyrte og testet. Nye
destruktive endringer skal ikke legges direkte inn i generell schema-oppretting
uten versjonssjekk.

## Regler for nye face-schema-endringer

- Øk `FACE_SCHEMA_VERSION`.
- Legg til en eksplisitt migreringsfunksjon fra forrige versjon.
- Ikke bland bred refaktorering med schema-endring.
- Behold scannede ansikter hvis det er praktisk og trygt.
- Ikke slett bilder.
- Skriv regresjonstest for migreringen.
- Skriv test for at gjeldende schema valideres.
- Hvis endringen kan miste brukerarbeid, vurder om den heller skal kreve en
  eksplisitt kommando eller backup.

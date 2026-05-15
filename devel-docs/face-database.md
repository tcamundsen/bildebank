# Face-database

Dette dokumentet beskriver den separate face-databasen `.bilder-faces.sqlite3`.
Den vanlige bildesamlingsdatabasen er `.bilder.sqlite3` og har egen
schema-versjon i `bilder/db.py`.

Face-databasen brukes for data som kan gjenskapes fra bildene, men som kan være
tidkrevende å bygge på nytt:

- resultat fra `face-scan`
- registrerte personer
- bekreftede ansiktskoblinger
- forslag fra `face-suggest`

## Dagens versjon

Dagens face-schema er `FACE_SCHEMA_VERSION = 3` i `bilder/face.py`.

Face-schema-versjonen lagres i:

```text
meta.schema_version
```

Nye face-databaser opprettes direkte som v3.

## Tabeller i v3

V3 inneholder disse hovedtabellene:

- `meta`
- `scanned_files`
- `faces`
- `persons`
- `person_faces`
- `face_suggestions`

`scanned_files` lagrer én rad per scannet bildefil.
`faces` lagrer ansiktsbokser og embeddings.
`persons` lagrer personnavn.
`person_faces` lagrer bekreftede koblinger mellom person og face-id.
`face_suggestions` lagrer beregnede forslag.

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

## Migrering v2 til v3

V2 hadde tabeller for gruppeflyten:

- `face_group_runs`
- `face_groups`
- `face_group_members`

Gruppeflyten er fjernet fordi den ga for dårlig datakvalitet. Brukeren skal i
stedet bekrefte gode enkeltansikter og deretter bruke `face-suggest`.

V3 fjerner gruppetabellene.

Migreringen skjer i `apply_face_schema()`:

- les `meta.schema_version`
- hvis versjonen er 0, opprett v3 direkte
- hvis versjonen er 2, kjør `migrate_face_schema_v2_to_v3()`
- sett `meta.schema_version = 3`
- valider gjeldende face-schema

`migrate_face_schema_v2_to_v3()` gjør bare dette:

```sql
DROP TABLE IF EXISTS face_group_members;
DROP TABLE IF EXISTS face_groups;
DROP TABLE IF EXISTS face_group_runs;
```

Dette sletter ikke bilder, scannede ansikter, personer, bekreftelser eller
forslag.

## Validering

Etter schema-oppretting eller migrering kjører `validate_current_face_schema()`.

For v3 skal legacy-gruppetabellene ikke finnes. Hvis en database sier
`schema_version=3`, men fortsatt inneholder gruppetabeller, skal programmet
feile tydelig i stedet for å slette tabellene lydløst.

Dette følger samme prinsipp som hoveddatabasen:

- eksplisitt migrering fra kjent eldre versjon
- ingen støtte for ukjent eldre layout
- avvis nyere schema-versjon enn programmet støtter
- ikke reparer en inkonsistent gjeldende versjon ved å slette data i stillhet

## Forskjell fra hoveddatabasen

Face-databasen migreres ikke av `bildebank migrate`.

Den migreres når face-kode åpner `.bilder-faces.sqlite3` med
`connect_face_db()`. Det er akseptabelt fordi face-databasen er en separat
side-database og v2 til v3 bare fjerner den gamle gruppeflyten.

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

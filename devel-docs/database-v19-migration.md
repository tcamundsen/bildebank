# Database v19: global unik SHA-256

Status: `schema_version=19` gjør `files.sha256` globalt unik og reparerer
eventuelle eksisterende duplikatrader automatisk.

## Invariant

Etter migreringen kan `files` inneholde høyst én rad per SHA-256, uavhengig av
om raden er aktiv eller har `deleted_at`. Indeksen er:

```sql
CREATE UNIQUE INDEX idx_files_sha256_unique
ON files(sha256);
```

De gamle indeksene `idx_files_sha256` og
`idx_files_sha256_active_unique` fjernes.

## Reparasjon av eksisterende duplikater

Migreringen fullføres også når duplikater finnes. For hver SHA-256-gruppe:

1. Radene sorteres med aktive rader først og deretter laveste `files.id`.
2. Den første raden i denne rekkefølgen som kan verifiseres mot fysisk
   størrelse og SHA-256, blir kanonisk. Hvis ingen fysisk kopi kan
   verifiseres, beholdes den første databaseraden.
3. Alle `file_sources` flyttes til den kanoniske raden.
4. Alle taggkoblinger slås sammen på den kanoniske raden.
5. Den kanoniske raden får systemtaggen
   `Bildebank: kontroller duplikatreparasjon`.
6. Sidecardata for de overflødige fil-ID-ene slettes. Eksisterende
   InsightFace-databaser sikkerhetskopieres før dette.
7. Hver overflødig fil legges i `pending_file_deletes` med forventet sti,
   størrelse og SHA-256 før den overflødige `files`-raden slettes.
8. Den globale unike indeksen opprettes i samme databasetransaksjon.

Kanonisk metadata beholdes uendret. Migreringen prøver ikke å gjette hvilke
kommentarer, datooverstyringer eller øvrige metadata som er best. Systemtaggen
gjør den mulige metadatausikkerheten synlig for brukeren.

En uavklart `pending_file_moves` for en overflødig fil-ID stopper
duplikatreparasjonen. Det hindrer at migreringen endrer eierskapet til en fil
mens en tidligere filflytting fortsatt har ukjent utfall.

## Fysisk opprydding

Database-reparasjonen committes før fysisk sletting. Migreringskommandoen
holder samlingslåsen og behandler deretter bare køpostene som v19 nettopp
opprettet.

Den eksisterende `pending_file_deletes`-kontrollen krever at:

- ingen `files`-rad lenger peker på stien
- stien ligger i en tillatt mediemappe i samlingen
- filen er en vanlig fil uten lenker
- størrelse og SHA-256 stemmer

En eksakt overflødig kopi slettes. En manglende kopi fjernes fra køen som
allerede borte. En endret, utrygg eller låst fil slettes ikke; køposten
beholdes med feilinformasjon og `migrate` skriver en advarsel. Dette er ikke en
tombstone-operasjon.

Ved avbrudd etter databasecommit ligger nok informasjon i
`pending_file_deletes` til at den vanlige oppryddingskommandoen trygt kan
fortsette senere.

## Brukermelding

Når duplikater er reparert, oppgir `bildebank migrate` hvor mange kanoniske
bilder som er merket, og ber brukeren kontrollere systemtaggen. Migreringen
går til v19 også når ingen duplikater finnes.

## Tester

Regresjonstestene dekker:

- v18 til v19 uten duplikater
- samling av `file_sources` og tagger
- systemtagg på den kanoniske raden
- prioritering av aktiv rad når kopien kan verifiseres
- valg av en annen fysisk gyldig kopi når første rad ikke stemmer
- global unik indeks for både aktive og slettede rader
- køføring og verifisert sletting av den overflødige fysiske kopien
- bevaring av en endret fysisk fil når SHA-256 ikke stemmer
- brukerbeskjeden fra `bildebank migrate`

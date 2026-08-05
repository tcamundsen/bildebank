# Database v23: stabile systemtaggnøkler

Status: `schema_version=23` skiller systemtaggens tekniske identitet fra det
synlige navnet.

## Schema

`tags` får den nullable kolonnen:

```sql
system_key TEXT
```

Ikke-null-verdier håndheves unike med den partielle indeksen:

```sql
CREATE UNIQUE INDEX idx_tags_system_key_unique
ON tags(system_key)
WHERE system_key IS NOT NULL;
```

De gjeldende systemtaggene har nøklene:

- `out_of_focus`
- `duplicate_repair_review`

Brukertagger har `system_key=NULL`. En systemtagg identifiseres i runtime med
`system_key`; `name` og `name_key` er visnings- og oppslagsverdier som kan
endres senere uten å endre systemtaggens betydning.

## Migrering

Migreringen finner de to eksisterende systemtaggene med dagens kanoniske navn
og setter riktig `system_key` på samme `tags`-rad. Den endrer derfor ikke
`tags.id` eller koblingene fra `file_tags`.

Migreringen er én databasetransaksjon og:

- leser eller endrer ingen mediefiler
- endrer ingen bruker- eller systemtaggnavn
- beholder alle eksisterende taggkoblinger
- oppretter den unike systemnøkkelindeksen

En ny database oppretter systemtaggene med nøkkel direkte. Seeding finner først
en eksisterende rad etter `system_key` og overskriver ikke navnet. Det gjør en
senere kontrollert omdøping mulig uten at schema-reparasjon setter det gamle
navnet tilbake.

## Runtime

`file_tags` fortsetter å koble alle taggtyper til bilder med lokal `tags.id`.
Kode med særbehandling av en bestemt systemtagg slår opp eller filtrerer etter
`system_key`, ikke norsk `name` eller `name_key`.

## Tester

Regresjonstestene dekker at:

- v22 til v23 beholder `tags.id` og `file_tags`
- migreringens dry-run ikke endrer databasen
- migreringen ikke leser eller endrer mediefiler
- systemspesifikk filtrering fortsetter å virke når visningsnavnet endres
- feil sent i migreringen ruller databaseendringen tilbake

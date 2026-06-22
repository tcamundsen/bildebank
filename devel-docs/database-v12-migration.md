# Database v12: pending_file_moves

Schema v12 legger til tabellen `pending_file_moves`. Tabellen er en kortlivet
arbeidsjournal for operasjoner som fysisk flytter filer inne i bildesamlingen.
Den er ikke historikk.

Omfattede operasjoner:

- `remove`
- `undelete`
- `refresh-metadata` når kommandoen flytter en fil til ny datomappe

Tabellen har disse kolonnene:

- `id`
- `file_id`
- `from_path`
- `to_path`
- `sha256`
- `operation`
- `state`
- `created_at`
- `updated_at`
- `completed_at`
- `last_error`

Flytteregler:

1. Kommandoen validerer kilde og mål som før.
2. Kommandoen skriver `pending_file_moves` med `state='prepared'` og committer.
3. Kommandoen flytter filen fysisk.
4. Kommandoen oppdaterer `files` og markerer pending-raden som `completed` i
   samme database-transaksjon.

Oppstartsrecovery kjøres tidlig for kommandoer som bruker en bildesamling,
inkludert `doctor`. `migrate` er unntatt fordi den må kunne åpne eldre schema.

Recovery behandler bare rader med `state='prepared'` og `completed_at IS NULL`:

- Hvis `from_path` finnes og `to_path` mangler, er flyttingen ikke utført.
  Raden markeres `aborted`; `files` endres ikke.
- Hvis `to_path` finnes og `from_path` mangler, verifiseres SHA-256. Ved match
  fullføres databaseoppdateringen og pending-raden markeres `completed`.
- Hvis begge stier finnes, ingen av stiene finnes, eller SHA-256 ikke matcher,
  stopper recovery med feil og kommandoen kjøres ikke.

`doctor` er ikke primær recovery-mekanisme. Normal CLI-oppstart kjører recovery
før `doctor` samler diagnose. Doctor rapporterer derfor normalt bare pending
flyttinger hvis recovery ikke kan fullføres automatisk eller databasen
inspiseres utenfor vanlig CLI-flyt.

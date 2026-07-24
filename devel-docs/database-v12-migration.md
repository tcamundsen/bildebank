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
4. Kommandoen oppdaterer `files` og sletter pending-raden i samme
   database-transaksjon.

Oppstartsrecovery kjøres tidlig for kommandoer som bruker en bildesamling,
inkludert `doctor`. `migrate` er unntatt fordi den må kunne åpne eldre schema.
`snapshot create` kjører recovery etter at snapshotoperasjonen har tatt
target-låsen, når hoveddatabasen kan åpnes trygt med gjeldende schema. En
manglende eller skadet hoveddatabase endres ikke; den eksisterende
recovery-snapshotflyten brukes da som før.

Recovery behandler bare rader med `state='prepared'` og `completed_at IS NULL`:

- Hvis `from_path` finnes og `to_path` mangler, er flyttingen ikke utført.
  Raden slettes; `files` endres ikke.
- Hvis `to_path` finnes og `from_path` mangler, verifiseres SHA-256. Ved match
  fullføres databaseoppdateringen og pending-raden slettes.
- Hvis begge stier finnes, ingen av stiene finnes, eller SHA-256 ikke matcher,
  stopper recovery med feil og kommandoen kjøres ikke.

Bare uavklarte rader skal beholdes. De har `state='prepared'`,
`completed_at IS NULL` og kan ha `last_error`. Tabellen er intern
arbeidstilstand, ikke brukerhistorikk. Eldre versjoner beholdt terminale
`completed`- og `aborted`-rader; hoveddatabasens v18-migrering sletter disse
én gang. Kolonnene for terminal tilstand beholdes i schemaet av
kompatibilitetshensyn.

Runtime-flytting skal bruke en no-clobber-operasjon som aldri erstatter en
eksisterende målsti. På filsystemer uten en egnet atomisk flytting kan
implementasjonen kopiere til en eksklusivt opprettet målfil, verifisere begge
kopiene og først deretter fjerne kildestien. Et avbrudd som etterlater begge
stier, forblir med vilje en uavklart recovery-tilstand.

`doctor` er ikke primær recovery-mekanisme. Normal CLI-oppstart kjører recovery
før `doctor` samler diagnose. Doctor rapporterer derfor normalt bare pending
flyttinger hvis recovery ikke kan fullføres automatisk eller databasen
inspiseres utenfor vanlig CLI-flyt.

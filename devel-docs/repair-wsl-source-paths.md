# Reparere WSL-stier i en Windows-import

`tools/repair_wsl_source_paths.py` er et målrettet reparasjonsverktøy for en
import som ved en feil er scannet både direkte i Windows og fra WSL.

Typisk symptom er at samme kilde vises to ganger for hvert bilde:

```text
google25: C:\Users\Tom\google\a.jpg
google25: /mnt/c/Users/Tom/google/a.jpg
```

Verktøyet endrer aldri bildefiler. Det arbeider bare med `sources` og
`file_sources` i hoveddatabasen.

## Sikkerhetsmodell

- Må kjøres direkte i Windows, ikke fra WSL.
- Er dry-run som standard.
- Behandler bare kilden angitt med `--name`.
- Konverterer `/mnt/c/...` og `\mnt\c\...` til `C:\...`.
- Sletter bare en WSL-rad når en Windows-rad for samme fysiske sti har samme
  `file_id`, SHA-256 og størrelse.
- Omskriver en WSL-rad til Windows-sti dersom Windows-motpart mangler.
- Kontrollerer at hver konverterte Windows-kildefil finnes og fortsatt har
  lagret størrelse og SHA-256.
- Stopper hele reparasjonen ved motstridende metadata.
- Tar en konsistent SQLite-backup før `--apply`.
- Kjører `foreign_key_check` og `integrity_check` før commit.

## Bruk

Kjør først dry-run fra PowerShell:

```powershell
python tools\repair_wsl_source_paths.py `
  --target "C:\Users\TA487\code\bilde-samling" `
  --name google25
```

For en fullstendig dobbelregistrering med 295 filer forventes omtrent:

```text
registrerte file_sources: 590
WSL-rader funnet: 295
verifiserte WSL-duplikater som fjernes: 295
WSL-rader som konverteres til Windows-sti: 0
```

Hvis rapporten er riktig, bruk:

```powershell
python tools\repair_wsl_source_paths.py `
  --target "C:\Users\TA487\code\bilde-samling" `
  --name google25 `
  --apply
```

Etter reparasjonen:

```powershell
cd "C:\Users\TA487\code\bilde-samling"
bildebank unimport --dry-run --name google25
```

Kontroller oppsummeringen. Kjør deretter vanlig `unimport` dersom målet er å
fjerne hele importen, og importer kildemappen på nytt fra Windows.

Databasebackupen får et navn som:

```text
.bilder.sqlite3.backup-before-wsl-source-repair-20260620-123456
```

Hent schema via bildebank_dev/get_schema_summary og se på hvordan files og file_sources henger sammen.

Les app-design.md ved endringer i import, database, filflytting/sletting, browser/server eller overordnet
produktatferd. For kommando-detaljer, bruk docs/<kommando>.md og relevant devel-docs først.

Dokumentasjonsfilene som finnes er README.md, README.linux.md
og docs/. Disse filene er skrevet for brukere som
ikke er programmerere, og som ikke er vant til å jobbe i et
terminalvindu. All brukerdokumentasjon skal bruke Windows-filnavn,
dvs for eksempel `C:\Users\Tom` og ikke `/home/tom`.

Hvis bruker starter prompt med "Spørsmål:" så skal du ikke endre kode.

# AI-regler for bildebanksystemet

- Sikkerhet for bilder er viktigere enn ryddighet og automatisering.
- Ingen kommando skal permanent slette bildefiler.
- "bildebank remove" skal bare flytte filer til deleted/ og markere metadata,
  ikke slette fysisk.
- Snapshots skal alltid ta med `deleted/`.
- Destruktive operasjoner skal ha dry-run når praktisk mulig.
- Endre bare det oppgaven ber om.
- Ikke refaktorer bredt uten eksplisitt beskjed.
- Skriv/oppdater tester for endret funksjonalitet.
- Hvis det innføres kommandoer som endrer bilder, så må det vurderes nøye
  hvordan det skal gjøres.
- Det er litt rotete med design-dokumenter nå, men det ryddes nå opp, og da
  plasseres brukerdokumentasjon i docs/ og dokumentasjon for utvikler og AI
  i devel-docs/.

## Python

- For full testsuite, prefer `python -m pytest -n auto`. Do not use `-q` for
  the full suite; it produces noisy subtest progress output with xdist.
- For focused tests while developing, use `python -m pytest <test-path>`
  without `-n`.

## Omfang
Gjør minimale, målrettede endringer. Ikke refaktorer kode som ikke er
direkte relatert til oppgaven du har fått.

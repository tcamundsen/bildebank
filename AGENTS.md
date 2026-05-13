Designet for appen er beskrevet i app-design.md

Dokumentasjonsfilene som finnes er README.md, README.linux.md
og docs/. Disse filene er skrevet for brukere som
ikke er programmerere, og som ikke er vant til å jobbe i et
terminalvindu. All brukerdokumentasjon skal bruke Windows-filnavn,
dvs for eksempel `C:\Users\Tom` og ikke `/home/tom`.

# AI-regler for bildebanksystemet

- Sikkerhet for bilder er viktigere enn ryddighet og automatisering.
- Ingen kommando skal permanent slette bildefiler.
- "bildebank remove" skal bare til deleted/ og markere metadata, ikke slette fysisk.
- backup skal kopiere også trash-can.
- Destruktive operasjoner skal ha dry-run når praktisk mulig.
- Endre bare det oppgaven ber om.
- Ikke refaktorer bredt uten eksplisitt beskjed.
- Skriv/oppdater tester for endret funksjonalitet.
- Hvis det innføres kommandoer som endrer bilder, f. eks rotering, så må det
  vurderes nøye hvordan det skal gjøres.
- Det er litt rotete med design-dokumenter nå, men det ryddes nå opp, og da plasseres
  brukerdokumentasjon i docs/ og dokumentasjon for utvikler og AI i devel-docs/.
  Hver kommando skal få en fil docs/kommandonavn.md som beskriver for brukeren
  hvordan den brukes.
- Hvis docs/ og devel-docs/ motsier hverandre skal devel-docs foretrekkes, og det
  må rapporteres om at filene motsier hverandre.

## Omfang
Gjør minimale, målrettede endringer. Ikke refaktorer kode som ikke er
direkte relatert til oppgaven du har fått.

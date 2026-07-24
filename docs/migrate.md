# migrate
<!-- CLI-HELP-START -->
```text
usage: bildebank migrate [valg]

Validerer og oppgraderer databasen etter en programoppdatering.

options:
  -h, --help  show this help message and exit
  --check     Vis hva migreringen vil gjøre uten å endre databasen
```
<!-- CLI-HELP-END -->

`migrate` oppgraderer Bildebank-databasen i en bildesamling til nytt format.
Kommandoen reparerer også manglende intern databasestruktur når databasen
allerede har gjeldende format. Dette kan for eksempel være manglende
taggtabeller, systemtagger, indekser eller samlingsidentitet.

## Når trenger du migrate?

Noen programoppdateringer endrer hvordan Bildebank lagrer informasjon i
databasen. Da kan Bildebank si fra om at databasen må migreres før du kan
fortsette.

Gjeldende Bildebank kan oppgradere databaser fra databaseformat v5 og nyere.
Format v4 og eldre er utfaset og støttes ikke av dagens migrering. Det skyldes
at disse formatene brukte en eldre måte å lagre stier til bilder på.

Hvis du mot formodning får beskjed om `schema_version=4` eller lavere, skal du
ikke endre versjonsnummeret manuelt. Behold hele bildesamlingen og eventuelle
snapshots uendret, og kontakt den som vedlikeholder Bildebank. En slik samling
må først konverteres kontrollert til v5 på en kopi, eller gjenopprettes fra et
snapshot som allerede bruker v5 eller nyere.

Gå til bildesamlingsmappen før du kjører kommandoen:

```powershell
cd "$HOME\BildeSamling"
bildebank migrate --check
bildebank migrate
```

## Hva gjør --check?

`--check` viser om databasen trenger migrering, uten å endre databasen.
Den viser også om intern struktur i en database med gjeldende versjon må
repareres.

Det er trygt å kjøre:

```powershell
bildebank migrate --check
```

## Backup

Når `bildebank migrate` faktisk endrer databasen, lager programmet en backup av
hoveddatabasen først. Ved migrering til v17 lager programmet også en backup av
hver InsightFace-database som finnes i bildesamlingen.

Hvis migreringen feiler, skal databasen ikke oppgraderes, og backupen beholdes.

## Migrering til v18

V18 rydder ferdigbehandlede rader fra Bildebanks interne
filflyttingsjournal. Uavklarte flyttinger beholdes, slik at Bildebank fortsatt
kan fullføre eller stoppe dem på en trygg måte.

## Migrering til v17

V17 rydder gamle OpenCLIP- og ansiktsdata for bilder som allerede er slettet,
eller som er helt fjernet fra hoveddatabasen av en eldre versjon. Data for
aktive bilder beholdes. Migreringen sletter eller flytter ingen bildefiler.

InsightFace-databasene sikkerhetskopieres før oppryddingen. Hvis oppryddingen
feiler, rulles databaseendringene tilbake og backupene beholdes.

## Migrering til v9

V9 legger til manuell dato på filer. Datoen lagres bare i Bildebank-databasen,
ikke i bildefilen. Browseren kan dermed vise et bilde etter korrigert dato
selv om filen fysisk ligger i den opprinnelige år/måned-mappen.

## Migrering til v8

V8 legger til finere H3-oppløsning for GPS-steder. Etter migreringen kan
Bildebank bruke H3-oppløsning 10 og 11, blant annet for manuell plassering av
bilder.

Eksisterende bilder med GPS-posisjon får de nye H3-feltene fylt ut fra
koordinatene som allerede ligger i databasen.

## Migrering til v7

V7 rydder gamle GPS-feilmeldinger som tidligere kunne bli svært lange. Etter
migreringen lagrer Bildebank bare en kort feilmarkør for filer der GPS-scanning
feilet.

Hvis databasefilen fortsatt er stor etter migreringen, kan du pakke den med
[`vacuum`](vacuum.md):

```powershell
bildebank vacuum
```

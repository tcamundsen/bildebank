# backup

`backup` lager eller oppdaterer en kopi av hele bildesamlingen.

```bash
bildebank backup plassering
```

`plassering` er mappen der backupen skal ligge. Bildebank lager selve
backupmappen med samme navn som bildesamlingen.

Eksempel:

```bash
bildebank backup D:\Backuper
```

Hvis bildesamlingen heter `bilde-samling`, blir backupen lagt her:

```text
D:\Backuper\bilde-samling
```

Backupen tar med hele bildesamlingen, inkludert databaser, HTML-filer og
`deleted/`.

## Når du bør bruke den

Bruk `backup` når du vil ha en kopi av hele samlingen til en annen disk eller
en annen mappe. Kommandoen er laget for å speile samlingen, ikke for å lage en
manuell zip-fil eller en enkelt eksport.

## Dry-run

Bruk `--dry-run` for å se hva kommandoen ville gjort uten å kopiere eller endre
noe.

```bash
bildebank backup --dry-run D:\Backuper
```

Dette er nyttig hvis du vil kontrollere at du peker på riktig backup-mappe før
du lar programmet skrive noe. Når `robocopy` eller `rsync` finnes, kjører
Bildebank verktøyets egen dry-run-funksjon slik at du får se hva verktøyet ville
gjort.

Du kan ikke legge en ny backup inni en eksisterende Bildebank-backup.

## Sikkerhet

Hvis backupmappen finnes fra før, må den allerede være merket som en Bildebank-
backup av samme bildesamling. Hvis ikke avbryter kommandoen.

Det gjør at Bildebank ikke speiler innhold inn i en vanlig mappe ved en feil.

Backupen har også en liten metadatafil, `.bildebank-backup.json`, i selve
backupmappen. Denne brukes til å kjenne igjen riktig backup. Filen er en del av
backupen, men den ligger ikke i bildesamlingen.

Mens backup kjører, låser Bildebank bildesamlingen slik at andre Bildebank-
kommandoer ikke endrer databasen eller filene samtidig.

## Hvis backup blir avbrutt

Hvis du avbryter med `Ctrl+C`, eller hvis backup-programmet feiler, kan
backupen være halvferdig. Da står metadata i backupmappen som `in-progress`.
Kjør samme backup-kommando på nytt for å fullføre og få backupen tilbake til
`complete`.

Ikke ta i bruk en backup som står som `in-progress`, med mindre du vet at du må
redde ut enkeltfiler manuelt.

Hvis PC-en krasjer eller mister strømmen, kan filen `.bildebank.lock` bli
liggende igjen i bildesamlingen. Da må du først kontrollere at ingen
Bildebank-kommando fortsatt kjører. Når du er sikker på det, kan du slette bare
`.bildebank.lock` og kjøre backup på nytt.

## Teknisk gjennomføring

På Windows bruker Bildebank `robocopy` når det finnes. På Linux og macOS bruker
programmet `rsync` når det finnes. Hvis verktøyet mangler, bruker Bildebank en
tregere Python-kopiering og skriver en tydelig advarsel.

Du trenger normalt ikke tenke på dette som bruker. Det viktigste er at backupen
blir oppdatert uten at kildesamlingen endres.

## Hvis du må ta i bruk en backup

Bildebank har ikke funksjon for å gjenopprette en hel bildesamling eller enkeltbilder
nå. Men for å gjenopprette en helt samling, så kopierer man backupen, for eksempel
`F:\foto-samling` til `C:\Bruker\Tom\foto-samling`, sletter filen `.bildebank-backup.json`
og så kan man ta i bruk bildesamlingen. **Ikke** kopier en backup over en eksisterende 
bildesamling.

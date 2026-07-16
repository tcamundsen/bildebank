# backup

<!-- CLI-HELP-START -->
```text
usage: bildebank backup [valg] plassering

Lag eller oppdater backup av bildesamlingen. NB: les dokumentasjonen for denne
kommandoen før du betror alle bildene dine til bildebank.

positional arguments:
  plassering  Eksisterende mappe der backupen skal ligge

options:
  -h, --help  show this help message and exit
  --dry-run   Vis hva som ville blitt gjort uten å kopiere eller endre filer
  --adopt     Registrer en eksisterende backupmappe som backup av denne
              bildesamlingen
```
<!-- CLI-HELP-END -->

> [!WARNING]
> `backup` lager en speiling av bildesamlingen.
> Når backup oppdateres, kan filer også slettes fra backupen.
> Ha derfor flere backup-disker som oppdateres på ulike tidspunkt.
> Bruk [`snapshot`](snapshot.md) når du vil bevare historiske versjoner og
> gjenopprette hele samlinger eller enkeltfiler med Bildebank.

`backup` lager eller oppdaterer en kopi av hele bildesamlingen.

Å lese bruksanvisning er kjedelig. Men for akkurat denne kommandoen
så **må** du lese hele dokumentet. Ellers risikerer du at du tror at du
har sikret bildesamlingen, bare for å oppdage at 10 år med
bilder mangler. Det kan også hende at du bestemmer
deg for at denne backup-funksjonen ikke er tilstrekkelig. Dette dokumentet
forklarer hvordan du bruker `bildebank backup`, hva det gjør og **hva
det ikke gjør**.

Programmet kjøres slik:

```bash
bildebank backup plassering
```

`plassering` er mappen der backupen skal ligge. Bildebank lager selve
backupmappen med samme navn som bildesamlingen.

Hvis bildesamlingen heter `bilde-samling`, dvs at den for eksempel
ligger i `C:\Users\Tom\bilde-samling` og du kjører dette:

```bash
bildebank backup D:\Backuper
```

så blir backupen lagt her:

```text
D:\Backuper\bilde-samling
```

Backupen tar med hele bildesamlingen, inkludert databaser, HTML-filer og
`deleted/`. Dette er med vilje: nylig slettede bilder skal også sikres fram
til trash-can er tømt manuelt. Interne runtime-filer som `.bildebank.lock` og
`.bildebank.log` tas ikke med.


## Når du bør bruke den

Bruk `backup` når du vil ha en kopi av hele samlingen til en annen disk eller
en annen mappe. Kommandoen er laget for å speile samlingen, ikke for å lage en
manuell zip-fil eller en enkelt eksport.

Det som er viktig å forstå, er at backupen blir en kopi av slik bildesamlingen
er nå. Og hvis du oppdaterer en backup, så oppdateres backupen til å være lik
bildesamlingen slik den er nå. Så hvis du har tatt en backup, sletter 100
bilder i bildesamlingen og legger til 10 nye bilder, og så oppdaterer backupen,
så slettes de 100 bildene fra backupen og de 10 nye bildene legges inn. **Les
den setningen en gang til!** La du merke til advarselen øverst på siden? Det er
dette det dreier seg om.

## Dry-run

Bruk `--dry-run` for å se hva kommandoen ville gjort uten å kopiere eller endre
noe. Det kan se litt kaotisk ut, men der vil du se hva som vil kopieres eller
slettes fra backup. Det vil komme bedre verktøy for å sjekke dette.

```bash
bildebank backup --dry-run D:\Backuper
```

Hvis du mener dette blir riktig, kjører du `backup` uten `--dry-run`:

```bash
bildebank backup D:\Backuper
```


Dette er nyttig hvis du vil kontrollere at du peker på riktig backup-mappe før
du lar programmet skrive noe. Når `robocopy` eller `rsync` finnes, kjører
Bildebank verktøyets egen dry-run-funksjon slik at du får se hva verktøyet ville
gjort.

Du kan ikke legge en ny backup inni en eksisterende Bildebank-backup.

## Registrere en gammel backup

Hvis du har en eksisterende backupmappe som mangler `.bildebank-backup.json`,
eller der metadatafilen mangler `backup_of`, vil vanlig `backup` avbryte.
Da kan du først be Bildebank vise en sammenligning:

```bash
bildebank backup --adopt --dry-run D:\Backuper
```

Kommandoen sammenligner filene Bildebank har i databasen med filene i
backupmappen. Den viser hvor mange filer som finnes med samme sti og størrelse,
hvor mange som mangler, hvor mange som har feil størrelse, og hvor mange ekstra
mediafiler som finnes i backupen.

Hvis rapporten ser riktig ut, kan du registrere backupmappen:

```bash
bildebank backup --adopt D:\Backuper
```

Da må du bekrefte ved å skrive `registrer backup`. Dette kopierer eller sletter
ingen bilder. Det skriver bare `.bildebank-backup.json` slik at Bildebank vet at
backupmappen hører til denne bildesamlingen.

Etter registrering kan du kjøre vanlig backup:

```bash
bildebank backup D:\Backuper
```

Vær oppmerksom på at vanlig backup er speiling. Filer som finnes i backupen, men
ikke i bildesamlingen, kan da bli slettet fra backupen.

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

Hvis backupen blir avbrutt, for eksempel med `Ctrl+C`, eller hvis
kopieringsverktøyet feiler, kan backupen være halvferdig. Da står metadata i
backupmappen som `in-progress`.  Kjør samme backup-kommando på nytt for å
fullføre og få backupen tilbake til `complete`.

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

## Gjenoppretting fra backup

Denne `backup`-kommandoen har ikke innebygd restore. Mirror-backupen er en vanlig
kopi av bildesamlingen og kan kopieres til en ny mappe ved en krise. **Ikke**
kopier en backup over en eksisterende bildesamling.

Den versjonerte [`snapshot`](snapshot.md)-løsningen har kontrollert restore av
hele samlinger og enkeltfiler. Velg `snapshot` når du trenger historiske
versjoner eller vil at Bildebank skal kontrollere innholdet under restore.

## Viktig å forstå

`backup` lager ikke historiske versjoner. Hvis du sletter eller ødelegger
filer i bildesamlingen, og senere oppdaterer backupen, så vil backupen også
bli oppdatert til den nye tilstanden. Derfor bør du ha flere backup-disker
som ikke oppdateres samtidig.

Versjonert [`snapshot`](snapshot.md) beskytter eldre tilstander bedre enn én
oppdatert speiling. Det erstatter likevel ikke flere medier, en frakoblet kopi
og en kopi utenfor boligen. Et annet dedikert backupverktøy kan også brukes for
å sikkerhetskopiere hele PC-en.

## Backup-medier

Gode råd:

- Ha flere backup-medier, som eksterne harddisker eller SSD-er
- Ikke oppdater alle samtidig
- Noen av dem kommer til å få feil.
  Jeg hadde en disk som feilet da jeg begynte å skrive på dette programmet.
- En backup-disk som alltid er koblet til PC-en beskytter ikke mot alle typer
  feil, som ransomware, alvorlige brukerfeil eller elektriske problemer.
- Ha minst en backup lagret utenfor hjemmet.

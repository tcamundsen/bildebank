# Versjonert backup med `snapshot`

<!-- CLI-HELP-START -->
```text
usage: bildebank snapshot <kommando> [valg]

Lag og kontroller versjonerte snapshots uten å endre dagens backup-mirror.

positional arguments:
  {create,list,problems,check,restore,restore-file}

options:
  -h, --help            show this help message and exit
```
<!-- CLI-HELP-END -->

`snapshot` lager versjonerte sikkerhetskopier av en bildesamling. Hver kjøring
bevarer en ny tilstand. Et eldre snapshot blir ikke gjort likt dagens samling
når du tar en ny sikkerhetskopi.

En databasebackup som Bildebank selv har laget før en migrering, tas også med.
Den vises som en egen type i dry-run, ikke som en ukjent fil.

Dette er forskjellig fra [`backup`](backup.md), som lager en speiling. En
speiling kan miste en gammel fil når speilingen oppdateres etter at filen har
forsvunnet fra bildesamlingen. Et publisert snapshot og innholdet det trenger,
blir aldri slettet eller overskrevet av Bildebank.

> [!IMPORTANT]
> Bruk flere backupdisker. Oppdater dem på forskjellige tidspunkter, og oppbevar
> minst én disk frakoblet og gjerne utenfor boligen. En tilkoblet og skrivbar
> backupdisk beskytter ikke alene mot tyveri, brann, ransomware eller alvorlige
> brukerfeil.

## Før du begynner

Du trenger en mappe på en annen disk enn bildesamlingen. I eksemplene ligger
bildesamlingen her:

```text
C:\Users\Tom\Familiebilder
```

Repositoryet, som er mappen der alle snapshots lagres, skal ligge her:

```text
E:\Bildebank-snapshots\Familiebilder
```

Du oppgir den eksakte repositorymappen. Bildebank legger ikke automatisk til
navnet på bildesamlingen. Den siste mappen kan mangle eller være helt tom ved
første kjøring. En ikke-tom, vanlig mappe blir aldri overtatt som repository.

Ikke endre, flytt eller slett filer inne i repositoryet med Filutforsker.
Repositoryet er laget for Bildebank og ser ikke ut som en vanlig bildekatalog.

## Hva som blir sikkerhetskopiert

Snapshotet tar blant annet med:

- alle databaseførte bilder og videoer
- bilder under `deleted\`
- hoveddatabasen og andre Bildebank-databaser
- vanlige filer i bildesamlingen som ikke finnes i databasen

Regenererbare thumbnails, genererte HTML-filer, aktive låsefiler og den aktive
loggfilen blir utelatt. De kan lages på nytt etter en gjenoppretting.

Alle databaseførte mediefiler blir lest og SHA-256-kontrollert ved hver reelle
snapshotkjøring. Dette kan ta tid selv når få nye filer må kopieres.

## Kontroller planen først

Åpne PowerShell i bildesamlingen og kjør alltid en dry-run først:

```powershell
bildebank snapshot create --dry-run E:\Bildebank-snapshots\Familiebilder
```

Dry-run skriver ingenting. Den viser blant annet antall filer, hva som er
utelatt, estimert ny datamengde og ledig plass. Dry-run beregner ikke SHA-256;
endelige avvik og den nøyaktige datamengden avgjøres under reell kjøring.
Mens planen bygges, viser Bildebank hvilken fase som pågår og løpende antall
filer på store bildesamlinger.

## Opprette et snapshot

Når planen ser riktig ut, kjører du:

```powershell
bildebank snapshot create E:\Bildebank-snapshots\Familiebilder
```

Mens snapshotet opprettes, viser Bildebank først at filinventaret bygges og
deretter løpende fremdrift for filer, byte og databaser. Store enkeltfiler
oppdaterer byteverdien mens de leses. Til slutt vises det at manifestet
publiseres. På store samlinger kan SHA-256-kontrollen fortsatt ta lang tid,
selv når de fleste objektene allerede finnes i repositoryet.

Du kan legge til en kommentar som ikke senere kan endres:

```powershell
bildebank snapshot create `
  --note "Før opprydding juli 2026" `
  E:\Bildebank-snapshots\Familiebilder
```

Snapshot kan også opprettes fra Bildebank-vinduet. Vinduet viser først den
samme skrivefrie planen og ber om bekreftelse før det skriver.
«Avbryt»-knappen kan brukes både mens planen bygges og mens snapshotet
opprettes. Avbrytelsen skjer kontrollert; tidligere snapshots endres ikke, og
ufullstendige data beholdes for kontroll. Hvis det siste, korte
publiseringstrinnet allerede har startet, fullføres det i stedet for å etterlate
snapshotet i en uklar tilstand.

Et resultat kan ha én av disse statusene:

- `complete`: Snapshotet ble fullført uten kjente avvik.
- `degraded`: Snapshotet ble publisert, men én eller flere filer eller
  tilleggsdatabaser hadde avvik. Bruk `snapshot problems`.
- `recovery`: Hoveddatabasen kunne ikke bekreftes. Lesbart innhold ble sikret
  for redning, men snapshotet kan ikke gjenopprettes som en vanlig hel
  bildesamling.

Ta vare på hele utskriften hvis statusen ikke er `complete`.

## Se snapshots og kildeavvik

Vis publiserte snapshots:

```powershell
bildebank snapshot list E:\Bildebank-snapshots\Familiebilder
```

Vis fil- og databaseavvik fra alle snapshots:

```powershell
bildebank snapshot problems E:\Bildebank-snapshots\Familiebilder
```

Du kan begrense listen til én snapshot-ID:

```powershell
bildebank snapshot problems `
  E:\Bildebank-snapshots\Familiebilder `
  8aba4e08-7661-4c90-b4e8-d82bc770dfe2
```

Problemlisten viser `entry_id`, sti og registrerte varianter. Dette er
opplysningene du trenger for å hente ut en problemfil med `restore-file`.

## Kontrollere repositoryet

En rask kontroll leser metadata, kontrollerer filstørrelser og ser etter
manglende objekter:

```powershell
bildebank snapshot check E:\Bildebank-snapshots\Familiebilder
```

En full kontroll leser alt lagret innhold og beregner SHA-256 på nytt:

```powershell
bildebank snapshot check E:\Bildebank-snapshots\Familiebilder --full
```

Full kontroll kan også startes fra Bildebank-vinduet. Den kan ta lang tid.
Bildebank endrer eller reparerer aldri repositoryet under kontrollen.

Kjør rask kontroll etter vanlige backupkjøringer. Kjør full kontroll med jevne
mellomrom, og før du legger bort en disk som skal være langtidskopi.

## Gjenopprette hele bildesamlingen

Finn snapshot-ID-en med `snapshot list`. Velg en helt ny mappe utenfor både
repositoryet og den opprinnelige bildesamlingen. Kontroller først planen:

```powershell
bildebank snapshot restore `
  E:\Bildebank-snapshots\Familiebilder `
  8aba4e08-7661-4c90-b4e8-d82bc770dfe2 `
  C:\Users\Tom\Familiebilder-gjenopprettet `
  --dry-run
```

Når planen er riktig, kjører du samme kommando uten `--dry-run`:

```powershell
bildebank snapshot restore `
  E:\Bildebank-snapshots\Familiebilder `
  8aba4e08-7661-4c90-b4e8-d82bc770dfe2 `
  C:\Users\Tom\Familiebilder-gjenopprettet
```

Bildebank viser planen igjen og ber deg skrive en eksakt bekreftelsestekst.
Målmappen må mangle eller være helt tom. En eksisterende fil eller ikke-tom
mappe blir aldri overskrevet.

Restore bygger og kontrollerer samlingen i en egen midlertidig mappe. Den
ferdige samlingen blir først synlig på endelig plass når alle filene,
databasen og `collection_id` er kontrollert. Ved et `degraded` snapshot blir
eventuelt observert avviksinnhold lagt i en separat recovery-mappe ved siden av
den gjenopprettede samlingen.

Originalen og restorekopien har samme `collection_id`. De representerer samme
logiske bildesamling. Ikke importer eller gjør andre endringer i begge som om de
var to uavhengige samlinger.

Etter restore bør du kontrollere kopien:

```powershell
bildebank --target C:\Users\Tom\Familiebilder-gjenopprettet doctor --deep
```

## Gjenopprette én fil

Bruk først dry-run. En normal snapshotsti skrives med `/`, slik den vises av
Bildebank, selv om resten av kommandoen bruker Windows-filnavn:

```powershell
bildebank snapshot restore-file `
  E:\Bildebank-snapshots\Familiebilder `
  8aba4e08-7661-4c90-b4e8-d82bc770dfe2 `
  C:\Users\Tom\Bildebank-eksport `
  --path "2010/01/IMAG0001.jpg" `
  --dry-run
```

Kjør uten `--dry-run` når planen er riktig. Bildebank viser valgt fil og ber
om en eksakt bekreftelse. Filen eksporteres under sin opprinnelige relative
sti. En eksisterende fil blir aldri overskrevet.

For en `recovery_only`-post bruker du `entry_id` fra `snapshot problems`:

```powershell
bildebank snapshot restore-file `
  E:\Bildebank-snapshots\Familiebilder `
  8aba4e08-7661-4c90-b4e8-d82bc770dfe2 `
  C:\Users\Tom\Bildebank-eksport `
  --entry-id e-000000000001 `
  --dry-run
```

Hvis både forventet og observert filvariant finnes, må du i tillegg velge
`--variant expected` eller `--variant observed`. En observert avviksvariant får
et kort hash-suffiks i filnavnet, slik at den ikke forveksles med forventet
innhold.

## Hvis en kommando blir avbrutt

Bildebank sletter ikke ufullstendige snapshot- eller restorefiler automatisk.
Feilmeldingen oppgir mappen som er bevart. Ikke slett eller flytt innholdet før
du har lest meldingen og kontrollert hva som ble fullført.

Et strømbrudd kan etterlate `.bildebank-repository.lock`. Kontroller først at
ingen Bildebank-prosess bruker repositoryet. Be om hjelp hvis du er usikker.
En lås skal aldri fjernes bare for å få en ny kommando til å starte.

En avbrutt enkeltfil-restore kan etterlate en ufullstendig eksportfil. Den blir
ikke overskrevet ved neste forsøk. Behold filen til feilen er undersøkt, og velg
eventuelt en ny eksportmappe for et nytt forsøk.

## En trygg rutine

1. Bruk minst to backupdisker og oppdater bare én om gangen.
2. Kjør `snapshot create --dry-run`.
3. Opprett snapshotet og kontroller at statusen er som forventet.
4. Kjør `snapshot check`.
5. Kjør `snapshot check --full` med jevne mellomrom.
6. Koble backupdisken fra PC-en når den ikke brukes.
7. Øv på hel restore til en ny testmappe før du trenger den i en krise.

Bildebank komprimerer eller krypterer ikke repositoryet. Bruk kryptering på
selve backupdisken hvis innholdet trenger beskyttelse.

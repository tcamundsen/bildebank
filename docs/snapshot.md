# Versjonerte sikkerhetskopier med `snapshot`

<!-- CLI-HELP-START -->
```text
usage: bildebank snapshot <kommando> [valg]

Opprett, kontroller og gjenopprett snapshots.

positional arguments:
  {create,list,problems,check,restore,restore-file}

options:
  -h, --help            show this help message and exit
```
<!-- CLI-HELP-END -->

`snapshot` lager en versjonert sikkerhetskopi av bildesamlingen. Hver gang du
oppretter et snapshot, bevares den nye tilstanden uten at eldre snapshots blir
endret. Du kan derfor gå tilbake til en tidligere tilstand selv om filer senere
blir flyttet eller fjernet fra den aktive samlingen.

En sikkerhetskopi av databasen som Bildebank har laget før en migrering, tas
også med. Den vises som en egen type i dry-run, ikke som en ukjent fil.

Et publisert snapshot og innholdet det trenger, blir aldri slettet eller
overskrevet av Bildebank.

> [!IMPORTANT]
> Bruk minst to separate disker til snapshots. Oppdater bare én disk om gangen,
> og oppbevar minst én disk frakoblet og gjerne utenfor boligen. En tilkoblet og
> skrivbar disk beskytter ikke alene mot tyveri, brann, ransomware eller
> alvorlige brukerfeil.

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

## Hva snapshotet inneholder

Snapshotet tar blant annet med:

- alle databaseførte bilder og videoer
- bilder under `deleted\`
- hoveddatabasen og andre Bildebank-databaser
- vanlige filer i bildesamlingen som ikke finnes i databasen

Regenererbare thumbnails og MP4-avspillingskopier under `video-previews`,
genererte HTML-filer, aktive låsefiler og den aktive loggfilen blir utelatt. De
kan lages på nytt etter en gjenoppretting. AVI-originalene tas med som andre
databaseførte videoer, og `deleted\` er fortsatt alltid med.

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

## Når bildesamlingen er flyttet

Repositoryet husker maskinen og den absolutte stien der bildesamlingen sist ble
bekreftet. Hvis du flytter samlingen, bytter PC eller lar en gjenopprettet kopi
overta som den aktive samlingen, stopper neste snapshot først. Dry-run viser
både det gamle og det nye arbeidsstedet uten å endre repositoryet.

Kontroller nøye at dette er samme logiske bildesamling som er flyttet. Ikke
bekreft hvis den gamle og den nye kopien skal brukes videre som to uavhengige
samlinger. Når du er sikker, kjører du den reelle kommandoen med det særskilte
bekreftelsesvalget:

```powershell
bildebank snapshot create `
  --confirm-moved-collection `
  E:\Bildebank-snapshots\Familiebilder
```

Bekreftelsen gjelder nøyaktig det gamle og nye arbeidsstedet som Bildebank
kontrollerer ved denne kjøringen. Hvis noe har endret seg siden planen, stopper
kommandoen og ber om en ny dry-run. Hoveddatabasen må være frisk og ha samme
`collection_id` som repositoryet. Recovery-snapshot kan ikke brukes til å
bekrefte en flytting.

I Bildebank-vinduet vises den samme advarselen med begge arbeidsstedene.
Knappen heter da **Bekreft flytting og opprett**. Ved bekreftelse oppdateres
bare repositoryets registrerte arbeidssted før snapshotet opprettes. Eldre
snapshots, repository-ID-en og samlings-ID-en endres ikke. Hvis den gamle
kopien brukes mot repositoryet senere, stopper Bildebank og krever samme
kontroll på nytt.

Et resultat kan ha én av disse statusene:

- `complete`: Snapshotet ble fullført uten kjente avvik.
- `degraded`: Snapshotet ble publisert, men én eller flere filer eller
  tilleggsdatabaser hadde avvik. Bruk `snapshot problems`.
- `recovery`: Hoveddatabasen kunne ikke bekreftes. Lesbart innhold ble sikret
  for redning, men snapshotet kan ikke gjenopprettes som en vanlig hel
  bildesamling.

Ta vare på hele utskriften hvis statusen ikke er `complete`.

## Flere disker og lagret status

Etter at et snapshot er publisert, husker denne Bildebank-installasjonen
repositoryet, tidspunktet og statusen. Bildebank-vinduet åpner den sist brukte,
tilgjengelige repositorymappen som standard. Dashboardet viser siste publiserte
snapshot for hvert repository som installasjonen kjenner.

Hvert repository har sin egen interne ID. Det betyr at flere USB-disker kan
bruke samme stasjonsbokstav og samme mappesti, for eksempel:

```text
F:\Bildebank-snapshots\Familiebilder
```

Opprett et eget repository i en tom mappe på hver disk. Da får diskene ulike
repository-ID-er, selv om Windows kaller alle diskene `F:` når de kobles til.

Ikke klon et repository og fortsett å bruke både originalen og klonen som to
selvstendige, skrivbare repositories. En klone beholder samme repository-ID som
originalen, og Bildebank kan derfor ikke alltid skille dem. En klone kan
beholdes som en frakoblet kopi som ikke oppdateres videre. En disk som skal
inngå selvstendig i vanlig rotasjon, skal initialiseres fra en tom mappe.

Den lagrede statusen ligger i Bildebanks lokale programdata, ikke i
repositoryet eller bildesamlingens database. Den er en praktisk oversikt, ikke
en integritetskontroll. Bruk fortsatt `snapshot check` og periodisk
`snapshot check --full`.

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
manglende objekter. Den åpner også den lagrede hoveddatabasekopien
skrivebeskyttet og kontrollerer at hver databaseført mediefil har en
tilsvarende snapshotpost med riktig sti, SHA-256 og størrelse:

```powershell
bildebank snapshot check E:\Bildebank-snapshots\Familiebilder
```

En full kontroll leser alt lagret innhold og beregner SHA-256 på nytt:

```powershell
bildebank snapshot check E:\Bildebank-snapshots\Familiebilder --full
```

Full kontroll kan også startes fra Bildebank-vinduet. Den kan ta lang tid.
Bildebank endrer eller reparerer aldri repositoryet under kontrollen.

Kjør rask kontroll etter at du har opprettet et nytt snapshot. Kjør full
kontroll med jevne mellomrom, og før du legger bort en disk som skal være
langtidskopi.

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
databasen og `collection_id` er kontrollert. Sluttkontrollen sammenligner også
databaseførte mediefiler med de faktiske restorefilene før samlingen
publiseres. Ved et `degraded` snapshot blir eventuelt observert avviksinnhold
lagt i en separat recovery-mappe ved siden av den gjenopprettede samlingen.

Hvis en databaseført fil bare kan bevares i recovery-mappen, mangler den
bevisst på ordinær plass. Restore publiserer da samlingen med en tydelig
advarsel og returnerer exitkode `3` for å vise at samlingen er ufullstendig.

Hvis snapshotet inneholder ansiktsdatabaser fra en absolutt
`face_recognition.database_dir`, legges databasene under
`.bildebank-faces\` i den gjenopprettede samlingen. Bildebank endrer ikke
konfigurasjonen automatisk. Restore viser derfor en advarsel om at innstillingen
må kontrolleres og eventuelt endres før face-funksjonene tas i bruk.

Originalen og restorekopien har samme `collection_id`. De representerer samme
logiske bildesamling. Ikke importer eller gjør andre endringer i begge som om de
var to uavhengige samlinger.

Etter restore bør du kontrollere kopien:

```powershell
bildebank --target C:\Users\Tom\Familiebilder-gjenopprettet doctor --deep
```

## Gjenopprette én fil

Du kan hente ut en vanlig bilde- eller videofil fra fanen **Snapshots** i
Bildebank-vinduet:

1. Velg snapshot-repositoryet øverst på fanen.
2. Klikk **Gjenopprett fil** og velg et snapshot fra listen.
3. Bla gjennom år, måned og filnavn. Filer som var fjernet fra samlingen, ligger
   under `deleted`, og filer uten kjent dato ligger under **Udatert**.
4. Kontroller eksportmappen og bekreft planen.

Bildebank foreslår denne eksportmappen:

```text
C:\Users\Tom\Downloads\Bildebank-gjenopprettet
```

Du kan velge en annen mappe. Filen eksporteres under sin opprinnelige relative
sti, for eksempel
`C:\Users\Tom\Downloads\Bildebank-gjenopprettet\2010\01\IMAG0001.jpg`.
Eksporten endrer ikke bildesamlingen eller snapshot-repositoryet, og en
eksisterende fil blir aldri overskrevet.

GUI-en viser vanlige filer under år/måned, **Udatert** og `deleted`. Bruk
kommandoen nedenfor for tekniske `recovery_only`-poster og andre avanserte
redningstilfeller.

### Fra kommandolinjen

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

Kjør uten `--dry-run` når planen er riktig. Bildebank viser valgt fil og spør
om du vil eksportere den. Svar `j` for å fortsette. Filen eksporteres under sin
opprinnelige relative sti. En eksisterende fil blir aldri overskrevet.

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

1. Bruk minst to separate disker og oppdater bare én om gangen.
2. Kjør `snapshot create --dry-run`.
3. Opprett snapshotet og kontroller at statusen er som forventet.
4. Kjør `snapshot check`.
5. Kjør `snapshot check --full` med jevne mellomrom.
6. Koble disken fra PC-en når den ikke brukes.
7. Øv på hel restore til en ny testmappe før du trenger den i en krise.

Bildebank komprimerer eller krypterer ikke repositoryet. Bruk kryptering på
selve lagringsdisken hvis innholdet trenger beskyttelse.

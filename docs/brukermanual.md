# Brukermanual for Bildebank

[Kommando-referanse](reference.md)
[Siste nytt: import-endringer](import-endringer.md)

Denne manualen er for deg som bruker Windows og PowerShell, og som allerede
har fulgt `README.md` og installert Bildebank.
Den viser de vanligste arbeidsflytene. Mer detaljer finner du i lenkene
øverst i dokumentet.

Eksemplene under bruker disse mappene:

- programmappen: `$HOME\kode\bildebank`
- bildesamlingsmappen: `$HOME\BildeSamling`
- en liten testkilde: `$HOME\Pictures\TestBilder`

Bytt ut mappenavnene hvis du har valgt andre steder.

## Om programmet

Noen ideer eller prinsipper som ligger bak programmet:

* Programmet skal ikke endre noen av mappene der det henter bilder fra. Alle bilder
  kopieres inn i en ny mappestruktur.
* Den nye bildesamlingen skal ikke avhenge av noe installert programvare. Man
  skal kunne kopiere det rett over på en minnepinne, plugge i en annen PC og
  bla i bildene, enten med de vanlige verktøyene som Windows eller Linux har,
  eller med en generert HTML-fil som visningsverktøy.
* Du skal kunne vite hvilken minnebrikke eller mappe alle bildene stammer fra.
* Vi vil unngå duplikater.

## Programmappen og bildesamlingsmappen

Bildebank bruker to forskjellige typer mapper.

**Programmappen** er mappen der selve Bildebank-programmet ligger. Hvis du har
fulgt `README.md`, heter den ofte:

```powershell
$HOME\kode\bildebank
```

I programmappen ligger selve programmet og filer som Bildebank trenger for å
starte, oppdatere seg og kjøre riktig. Til vanlig trenger du ikke åpne eller
endre filene i programmappen.

**Bildesamlingsmappen** er mappen der din nye bildebank skal ligge. Det er her
Bildebank lager databasen, årsmappene, månedsmappene og `index.html`.

Eksempel:

```powershell
$HOME\BildeSamling
```

Bildesamlingsmappen kan ikke ligge inni programmappen. Bildebank-programmet
vil forhindre det fra å opprette en bildesamling i programmappen

Bildesamlingsmappen er en mappe Bildebank styrer. Ikke flytt, gi nytt navn til
eller slett filer inne i denne mappen manuelt, med mindre manualen sier det.
Bruk Bildebank-kommandoer som `remove` når du vil fjerne noe fra samlingen.

Du kan åpne og se på filene i bildesamlingsmappen, men ikke rydd manuelt i
årsmappene og månedsmappene. Da kan databasen og filene komme ut av synk.

Nedenfor viser jeg trinn for trinn hvordan du kan lage en samling med
bilder og vise dem i nettleser. Når du er klar for å gå i gang for alvor,
så finner du lenke til detaljer om alle kommandoene øverst i dette dokumentet.

## Opprett en testmappe med noen få bilder

Lag en mappe som heter TestBilder i Bilder-mappen din.
Kopier noen få bilder inn i denne mappen. Bruk gjerne 5–10 bilder første gang.
Dette gjør du på vanlig måte med mus og Filutforsker.


## Først et lite lynkurs i PowerShell

Dette programmet er basert på at det kjøres i et terminalvindu
og at vi utfører kommandoer ved å skrive tekst. Derfor bruker vi
PowerShell.

Åpne PowerShell slik:

1. Trykk på Start-menyen i Windows.
2. Skriv `PowerShell`.
3. Åpne `Windows PowerShell`.

På samme måte som Filutforsker viser viser filene eller bildene i en mappe,
så har PowerShell tilgang til en mappe. Når du starter PowerShell, så ser kanskje
de siste linjene i vinduet slik ut:

```powershell
Install the latest PowerShell
Windows PowerShell
Copyright (C) Microsoft Corporation. All rights reserved.

Install the latest PowerShell for new features and improvements! https://aka.ms/PSWindows

PS C:\Users\Tom>
```

`Tom` vil være erstattet av ditt brukernavn, og det kan hende at det er en annen mappe.
`PS` er nok bare en påminnelse om at du kjører Powershell, og `C:\Users\Tom>` forteller
meg at PowerShell ser på mappen `C:\Users\Tom`.

Hvis det i denne mappen finnes en mappe som heter kode, så bruker vi kommandoen `cd`
(som er forkortelse for Change Directory) til å gå inn i den mappen:

```powershell
PS C:\Users\Tom> cd kode
PS C:\Users\Tom\kode>
```

Vi kan da jobbe med filene i denne mappa, bare ved å skrive navnet deres, i stedet
for å også ta med navnet på mappen de ligger i.
Du ser at at det som står til venstre for markøren har endret seg og viser at du nå er
mappen kode. For å gå ut av mappen, dvs opp et nivå skriver du `cd ..`:

```powershell
PS C:\Users\Tom> cd kode
PS C:\Users\Tom\kode> cd ..
PS C:\Users\Tom>
```

Men i alle eksemplene i denne manualen, så tar jeg ikke med alt som viser
hvilken mappe, dvs `PS C:\Users\Tom\kode` i dette eksempelet, for du har jo
sannsynligvis noe annet som står der.

## Tilbake til Bildebank

Sjekk at Bildebank starter:

```powershell
bildebank
```

Programmet vil da skrive ut en liste over kommandoer som kan kjøres.

`$HOME` er en variabel som vet hvor hjemmemappen for din bruker er på PC-en.
For meg så betyr de to linjene her det samme:

```powershell
$HOME
C:\Users\Tom
```
Hvis bildesamlingsmappen ikke finnes ennå, kan du lage den først:

```powershell
cd $HOME
mkdir BildeSamling
```

Når du skal jobbe med bildesamlingen, går du til bildesamlingsmappen:

```powershell
cd BildeSamling
```

Eksempelet over fungerer fordi vi nettopp har gått til `$HOME` i PowerShell i
eksempelet overfor der. Hvis vi ikke hadde gjort det, ville vi skrevet
den fulle adressen til mappen, og gått dit:

```powershell
cd $HOME\BildeSamling
```

## Opprett målmappe

Kommandoen `create` gjør den mappen du står i til en Bildebank-målmappe. Punktum
betyr "denne mappen".

Kjør dette fra bildesamlingsmappen:

```powershell
bildebank create .
```

Bildebank oppretter databasen sin i bildesamlingsmappen. Etterpå er dette
mappen du vanligvis står i når du bruker Bildebank.

## Importer en kilde

En kildemappe er en mappe der du allerede har bilder eller videoer som skal
importeres. Det kan være en vanlig mappe på PC-en, en USB-brikke, en CD, et
minnekort eller en ekstern disk.

Hver import skal ha et navn. Navnet bruker du senere hvis du vil se hvor bildene
kom fra eller angre importen.

Vi skal nå prøve å importere mappen TestBilder som du opprettet. Windows kan
være satt opp på mange måter, så det er mulig at `$HOME\Pictures\TestBilder`
ikke finner testmappen din. Men vi forsøker først standard måte:

Tørrtest testmappen først:

```powershell
bildebank import --name "TestBilder" --dry-run "$HOME\Pictures\TestBilder"
```

Hvis du får feilmelding eller noe som tyder på at vi ikke finner mappen
høyreklikk mappen i Filutforsker og velg Kopier som bane. Lim inn banen i
kommandoen i stedet for `$HOME\Pictures\TestBilder`.

Da viser Bildebank hva programmet ville importert, uten å kopiere filer og uten
å endre databasen.

Se gjennom listen. Hvis den ser riktig ut, kan du importere på ordentlig:

```powershell
bildebank import --name "TestBilder" "$HOME\Pictures\TestBilder"
```

For en annen mappe bruker du samme mønster:

```powershell
bildebank import --name "Julen2022" "C:\Users\Tom\Julen2022"
```

Bruk hermetegn rundt stier. Det er spesielt viktig hvis mappenavnet inneholder
mellomrom.

Bildebank kopierer støttede bilder og videoer inn i bildesamlingsmappen og
plasserer dem etter dato, for eksempel i mapper som `2024\01`.

På slutten skriver programmet en oppsummering, for eksempel:

```text
Oppsummering: scannet=10, importert=10, duplikater=0, eksisterende=0, dekket=0, navnekollisjoner=0, feil=0
```

Hvis du prøver å bruke samme navn på nytt, stopper Bildebank. Velg et nytt navn
for en ny import.

## Import fra CD, USB og flyttbare medier

For CD-er, USB-disker, minnekort og andre flyttbare medier bruker du også
`import`.

Gi mediet et stabilt navn med `--name`. Bruk for eksempel teksten som står på
CD-en, navnet på USB-disken, eller et annet navn du vil kjenne igjen senere.

Tørrtest først:

```powershell
bildebank import --name "Familie-CD-2004" --dry-run E:\
```

Importer på ordentlig:

```powershell
bildebank import --name "Familie-CD-2004" E:\
```

Bytt ut `E:\` med stasjonen eller mappen der mediet finnes hos deg. Grunnen til
at `--name` er viktig, er at samme stasjonsbokstav kan brukes av forskjellige
CD-er og USB-disker på forskjellige tidspunkt.

## Se status

Kjør:

```powershell
bildebank status
```

Status viser blant annet totalt antall importerte filer, hvor mange som er
bilder og videoer, og hvor datoen kom fra:

- `metadata`: dato funnet i bilde- eller videometadata
- `filename`: dato tolket fra filnavnet
- `mtime`: dato fra filens endringstidspunkt
- `unknown`: ingen sikker dato funnet

## Lag HTML-visning

Kjør:

```powershell
bildebank make-browser
```

Bildebank lager da filen `index.html` i bildesamlingsmappen.

Åpne `index.html` i nettleseren for å bla i de importerte bildene og videoene.
Du kan dobbeltklikke på filen i Filutforsker, eller åpne den fra nettleseren.

Hvis du importerer flere filer senere, kjør `make-browser` på nytt for å lage en
oppdatert `index.html`.

For å unngå at månedsoversikten blir for tung å laste kan antall bilder begrenses
med `--month-preview-limit`:

```powershell
bildebank make-browser --month-preview-limit 40
```

HTML-visningen åpnes ved å dobbeltklikke på `index.html` med Filutforsker
i Windows, eller med `open-browser`:

```powershell
bildebank open-browser
```

Når HTML-visningen er åpen, kan du bla med tastaturet:

- Pil venstre/høyre: forrige eller neste bilde/video
- Pil opp/ned: forrige eller neste måned
- Page Up/Page Down: forrige eller neste år

Se også [`make-browser`](make-browser.md) og [`open-browser`](open-browser.md).

## Se registrerte kilder

Kjør:

```powershell
bildebank list-sources
```

Listen viser kildene Bildebank kjenner til. Hver kilde har et navn, status,
importtidspunkt og filstien den ble importert fra.

Dette er nyttig når du vil kontrollere hva som allerede er registrert og om en
kilde er importert.

## Finne programmappen og bildesamlingen igjen

Hvis du er usikker på hvor Bildebank ligger, eller hvor bildesamlingen din ble
opprettet, kan du kjøre:

```powershell
bildebank where-is
```

Kommandoen viser:

- hvor Bildebank-programmet ligger
- hvor Bildebank lagrer sin lille oversikt over kjente bildesamlinger
- hvilken mappe PowerShell står i akkurat nå
- hvilke bildesamlingsmapper Bildebank kjenner til

Når du oppretter en ny bildesamling med `create`, lagres den automatisk i denne
oversikten. Hvis du allerede hadde en bildesamling fra før, blir den også lagt
til automatisk neste gang du bruker Bildebank med den målmappen.

Hvis `where-is` viser en bildesamlingsmappe, kan du kopiere `cd`-linjen som
kommandoen foreslår, for eksempel:

```powershell
cd "C:\Users\Tom\BildeSamling"
```

## Angre import av en kilde

Hvis du har importert feil mappe, CD eller USB-disk, kan du bruke `unimport`
for å angre akkurat den importen.

Tørrtest først:

```powershell
bildebank unimport --dry-run --name "TestBilder"
```

Eksempel:

```powershell
bildebank unimport --name "TestBilder"
```

Bruk samme navn som du brukte da du importerte. Ikke bruk stien til mappen,
USB-disken eller CD-en.

Med `--dry-run` kontrollerer Bildebank filene og viser hva som ville blitt
fjernet, men endrer ikke databasen og sletter ingen filer.

`unimport` er en kraftig kommando, fordi den kan fjerne filer fra den aktive
bildesamlingen. Bruk den derfor bare når du er sikker på at du har valgt riktig
kilde.

Før Bildebank endrer noe, kontrollerer programmet at alle filene fra denne
kilden fortsatt finnes i kilden, og at de er helt identiske med det som ble
importert. Hvis en fil mangler eller er endret, stopper programmet uten å gjøre
endringer. Grunnen er at du skal kunne importere samme kilde på nytt senere.

Hvis et bilde også finnes i andre kilder, blir bildet liggende i
bildesamlingsmappen. Da fjernes bare koblingen til kilden du angrer. Hvis bildet
bare kom fra denne ene kilden, fjernes det fra den aktive bildesamlingen.

Før kommandoen gjennomføres, viser Bildebank en oppsummering, for eksempel:

```text
Kilde: TestBilder
Registrerte kildefiler kontrollert: 179
Filer som fjernes fra aktiv samling: 142
Filer som blir liggende fordi de også finnes i andre kilder: 37
Skriv "ja, det vil jeg" for å gjennomføre unimport:
```

Les oppsummeringen nøye. For å gjennomføre må du skrive nøyaktig:

```text
ja, det vil jeg
```

Hvis du skriver noe annet, eller bare trykker Enter, avbryter Bildebank uten å
endre noe.

Etter en `unimport` kan du lage HTML-visningen på nytt:

```powershell
bildebank make-browser
```

Da blir `index.html` oppdatert slik at den viser samlingen etter at importen er
angret.

Når du angrer en navngitt import, fjerner Bildebank også denne kilden fra
kildelisten.

## Finn navnekollisjoner

En navnekollisjon betyr at flere importerte filer ville hatt samme filnavn i
samme målmappe. Bildebank beholder filene, men lagrer noen av dem med justert
navn. Dette er egentlig ikke et problem, men hvis du feilsøker, eller hvis du
tror at du har for eksempel samme bilde i forskjellig oppløsning fra to kilder,
med samme filnavn, så kan dette være nyttig å se på.

List navnekollisjoner:

```powershell
bildebank conflicts
```

Se detaljer for en bestemt importert fil i bildesamlingen:

```powershell
bildebank show-conflict "2024\01\IMG_0001.jpg"
```

Bytt ut stien med en fil fra listen. Kommandoen viser hvilke kildefiler som
hører til samme kollisjon, hvor de ble importert fra, filstørrelse og hash.

## Finn filer uten metadata-dato

Kjør:

```powershell
bildebank non-metadata --with-source
```

Denne listen viser filer der datoen ikke kom fra metadata. Med `--with-source`
viser Bildebank også hvilken original kildefil den importerte filen i
bildesamlingen kom fra.

Dette er nyttig når du vil kontrollere filer som er plassert etter filnavn,
filens endringstidspunkt eller ukjent dato.

## Se importfeil

Kjør:

```powershell
bildebank errors
```

Dette viser registrerte feil som fortsatt ikke er løst.

For å se både uløste og løste feil:

```powershell
bildebank errors --include-resolved
```

Løste feil kan for eksempel være feil som senere ble rettet ved en ny kontroll
eller ny import.

## Slette en importert fil

Hvis du vil fjerne en importert fil fra den aktive bildebanken, bruk
`remove`. Kommandoen sletter ikke filen helt. Den flytter filen til
`deleted`-mappen i bildesamlingsmappen og markerer den som slettet i databasen.

Eksempel:

```powershell
bildebank remove "2024\01\IMG_0001.jpg"
```

Se filer som er markert som slettet:

```powershell
bildebank list-removed
```

## Hente oppdateringer

For å sikre at du har siste versjon av programmet, kjør:

```powershell
bildebank update
```

Hvis `bildebank update` ikke virker, kan du kjøre oppdateringsscriptet direkte:

```powershell
cd $HOME\kode\bildebank
powershell.exe -ExecutionPolicy Bypass -File .\update.ps1
```

Hvis det heller ikke virker, kan du gjøre det manuelt:

```powershell
cd $HOME\kode\bildebank
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e .
```

Deretter kan du bruke programmet som før.

### Migrere gammel database

Noen programoppdateringer kan kreve at databasen i bildesamlingsmappen
oppgraderes før du kan importere eller gjøre andre endringer. Hvis Bildebank
ber om det etter `bildebank update`, gå til bildesamlingsmappen og kjør:

```powershell
cd $HOME\BildeSamling
bildebank migrate
```

Migrering til databaseformat v4 gjelder bare brukere som har opprettet
bildesamlingsdatabasen med en eldre versjon av Bildebank. Nye databaser
opprettet med Bildebank fra og med denne versjonen bruker v4-formatet allerede
og trenger ikke denne migreringen.

Du kan kontrollere hva migreringen vil gjøre uten å endre databasen:

```powershell
bildebank migrate --check
```

Når `bildebank migrate` faktisk kjøres, lager programmet en backup av
`.bilder.sqlite3` før databasen endres. Hvis migreringen feiler, skal databasen
ikke oppgraderes, og backupen beholdes.


## Sikkerhet og backup

Bildebank er ikke en backup-løsning. Programmet organiserer og kopierer bilder
og videoer inn i en ny samling, men det erstatter ikke sikkerhetskopier.

Ikke slett originalkilder etter import bare fordi Bildebank har kopiert filene.
Kontroller først at importen ser riktig ut, at `index.html` viser det du
forventer, og at bildesamlingen er sikkerhetskopiert til mer enn ett trygt
sted.

En enkel regel er 3-2-1-regelen: ha minst 3 kopier av viktige filer, på minst
2 forskjellige lagringsmedier, og minst 1 kopi et annet sted enn hjemme.

Behold gamle kilder til du er sikker på at den nye samlingen er kontrollert og
sikkerhetskopiert.

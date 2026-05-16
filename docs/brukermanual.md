# Brukermanual for Bildebank

[Kommando-referanse](reference.md)

[Siste nytt: import-endringer](import-endringer.md)

Denne manualen er for deg som bruker Windows og PowerShell, og som allerede
har fulgt [README](README.md) og installert Bildebank. Den viser de vanligste
arbeidsflytene. Du finner mer detaljerte beskrivelser i lenkene øverst i
dokumentet.

Eksemplene under bruker disse mappene:

- programmappen: `$HOME\kode\bildebank`
- bildesamlingsmappen: `$HOME\BildeSamling`
- en liten testkilde: `$HOME\Pictures\TestBilder`

Bytt ut mappenavnene hvis du har valgt andre steder.

## Om programmet

Noen prinsipper som ligger til grunn for Bildebank:

- Programmet skal ikke endre mappene det henter bilder fra. Alle bilder
  kopieres inn i en ny mappestruktur.
- Den nye bildesamlingen skal kunne brukes uten at Bildebank er installert.
  Du skal kunne kopiere samlingen til en annen PC og bla i bildene med vanlige
  filverktøy eller med en generert HTML-fil.
- Du skal kunne se hvilken mappe, CD, USB-disk eller minnebrikke bildene
  opprinnelig kom fra.
- Programmet skal unngå å importere duplikater.

## Programmappen og bildesamlingsmappen

Bildebank bruker to forskjellige typer mapper.

**Programmappen** er mappen der selve Bildebank-programmet ligger. Hvis du har
fulgt `README.md`, heter den ofte:

```powershell
$HOME\kode\bildebank
```

I programmappen ligger programkoden og filene Bildebank trenger for å starte,
oppdatere seg og kjøre riktig. Til vanlig trenger du ikke åpne eller endre
filene i programmappen.

**Bildesamlingsmappen** er mappen der bildebanken din ligger. Det er her
Bildebank lager databasen, årsmappene, månedsmappene og andre genererte filer.

Eksempel:

```powershell
$HOME\BildeSamling
```

Bildesamlingsmappen kan ikke ligge inni programmappen. Bildebank hindrer deg i
å opprette en bildesamling der.

Bildesamlingsmappen styres av Bildebank. Ikke flytt, gi nytt navn til eller
slett filer inne i denne mappen manuelt, med mindre manualen sier det. Bruk
Bildebank-kommandoer som `remove` når du vil fjerne noe fra samlingen.

Du kan åpne og se på filene i bildesamlingsmappen, men ikke rydd manuelt i
årsmappene og månedsmappene. Da kan databasen og filene komme ut av synk.

## Opprett en testmappe med noen få bilder

Lag en mappe som heter `TestBilder` i Bilder-mappen din. Kopier inn noen få
bilder, gjerne 5–10 bilder første gang. Dette gjør du på vanlig måte i
Filutforsker.

## Først et lite lynkurs i PowerShell

Bildebank kjøres fra et terminalvindu. På Windows bruker vi PowerShell.

Åpne PowerShell slik:

1. Trykk på Start-menyen i Windows.
2. Skriv `PowerShell`.
3. Åpne `Windows PowerShell`.

PowerShell arbeider i én mappe om gangen, omtrent som Filutforsker viser én
mappe om gangen. Når du starter PowerShell, kan de siste linjene i vinduet se
slik ut:

```powershell
Install the latest PowerShell
Windows PowerShell
Copyright (C) Microsoft Corporation. All rights reserved.

Install the latest PowerShell for new features and improvements! https://aka.ms/PSWindows

PS C:\Users\Tom>
```

`Tom` vil være erstattet av ditt brukernavn. `PS` viser at du bruker
PowerShell, og `C:\Users\Tom>` viser hvilken mappe PowerShell står i.

Hvis denne mappen inneholder en mappe som heter `kode`, kan du gå inn i den med
kommandoen `cd`:

```powershell
PS C:\Users\Tom> cd kode
PS C:\Users\Tom\kode>
```

Da kan du arbeide med filene i denne mappen ved å skrive filnavnet, uten å ta
med hele stien. For å gå opp ett nivå skriver du `cd ..`:

```powershell
PS C:\Users\Tom> cd kode
PS C:\Users\Tom\kode> cd ..
PS C:\Users\Tom>
```

I eksemplene videre tar jeg ikke med hele PowerShell-ledeteksten, siden den
sannsynligvis ser annerledes ut hos deg.

## Sjekk at Bildebank starter

Kjør:

```powershell
bildebank
```

Programmet skal da skrive ut en liste over kommandoer.

`$HOME` er en variabel som peker på hjemmemappen din. For meg betyr disse to
linjene det samme:

```powershell
$HOME
C:\Users\Tom
```

## Opprett bildesamlingsmappen

Hvis bildesamlingsmappen ikke finnes ennå, kan du lage den slik:

```powershell
cd $HOME
mkdir BildeSamling
cd BildeSamling
```

Kommandoen `create` gjør mappen du står i til en bildesamling. Punktum betyr
«denne mappen».

Kjør dette fra bildesamlingsmappen:

```powershell
bildebank create .
```

Bildebank oppretter databasen sin i bildesamlingsmappen. Etterpå er dette
mappen du vanligvis står i når du bruker Bildebank.

Hvis du allerede har laget bildesamlingsmappen, kan du gå dit med hele stien:

```powershell
cd $HOME\BildeSamling
```

## Importer en kilde

En kilde er en mappe, CD, USB-disk, minnebrikke eller ekstern disk med bilder
eller videoer som skal importeres.

Hver import skal ha et navn. Navnet bruker du senere hvis du vil se hvor
bildene kom fra, eller hvis du vil angre importen.

Vi prøver først med testmappen `TestBilder`:

```powershell
bildebank import --name "TestBilder" --dry-run "$HOME\Pictures\TestBilder"
```

`--dry-run` betyr at Bildebank viser hva programmet ville ha importert, uten å
kopiere filer og uten å endre databasen.

Hvis Bildebank ikke finner mappen, høyreklikker du mappen i Filutforsker og
velger **Kopier som bane**. Lim inn banen i kommandoen i stedet for
`$HOME\Pictures\TestBilder`.

Se gjennom listen. Hvis den ser riktig ut, kan du importere på ordentlig:

```powershell
bildebank import --name "TestBilder" "$HOME\Pictures\TestBilder"
```

For en annen mappe bruker du samme mønster:

```powershell
bildebank import --name "Julen2022" "C:\Users\Tom\Julen2022"
```

Bruk hermetegn rundt stier, spesielt hvis mappenavnet inneholder mellomrom.

Bildebank kopierer støttede bilder og videoer inn i bildesamlingsmappen og
plasserer dem etter dato, for eksempel i mapper som `2024\01`.

På slutten skriver programmet en oppsummering, for eksempel:

```text
Oppsummering: scannet=10, importert=10, duplikater=0, eksisterende=0, dekket=0, navnekollisjoner=0, feil=0
```

Hvis du prøver å bruke samme importnavn på nytt, stopper Bildebank. Velg et
nytt navn for en ny import.

## Import fra CD, USB og flyttbare medier

For CD-er, USB-disker, minnekort og andre flyttbare medier bruker du også
`import`.

Gi mediet et stabilt navn med `--name`. Bruk for eksempel teksten som står på
CD-en, navnet på USB-disken eller et annet navn du vil kjenne igjen senere.

Tørrtest først:

```powershell
bildebank import --name "Familie-CD-2004" --dry-run E:\
```

Importer på ordentlig:

```powershell
bildebank import --name "Familie-CD-2004" E:\
```

Bytt ut `E:\` med stasjonen eller mappen der mediet finnes hos deg. `--name`
er viktig fordi samme stasjonsbokstav kan brukes av forskjellige CD-er og
USB-disker på forskjellige tidspunkt.

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

## Lag statisk HTML-visning

Kjør:

```powershell
bildebank make-browser
```

Bildebank lager da filen `index.html` i bildesamlingsmappen. Åpne filen ved å
dobbeltklikke på den i Filutforsker, eller fra PowerShell:

```powershell
.\index.html
```

Når HTML-visningen er åpen, kan du bla med tastaturet:

- Pil venstre/høyre: forrige eller neste bilde/video
- Pil opp/ned: forrige eller neste måned
- Page Up/Page Down: forrige eller neste år

Hvis du importerer flere filer senere, må du kjøre `make-browser` på nytt for å
lage en oppdatert `index.html`.

For å gjøre månedsoversikten lettere å laste kan du begrense antall bilder som
vises i månedsoversikten:

```powershell
bildebank make-browser --month-preview-limit 40
```

Den statiske HTML-visningen kan fungere på andre PC-er uten at Bildebank er
installert, så lenge `index.html` kopieres sammen med bildene i undermappene.

Flere funksjoner finnes i den serverbaserte bildebrowseren som startes med
[`run-server`](run-server.md).


## Se registrerte kilder

Kjør:

```powershell
bildebank list-sources
```

Listen viser kildene Bildebank kjenner til. Hver kilde har et navn, status,
importtidspunkt og filstien den ble importert fra.

Dette er nyttig når du vil kontrollere hva som er registrert, og om en kilde er
importert.

## Finne programmappen og bildesamlingen igjen

Hvis du er usikker på hvor Bildebank ligger, eller hvor bildesamlingen din ble
opprettet, kan du kjøre:

```powershell
bildebank where-is
```

Kommandoen viser:

- hvor Bildebank-programmet ligger
- hvor Bildebank lagrer oversikten over kjente bildesamlinger
- hvilken mappe PowerShell står i nå
- hvilke bildesamlingsmapper Bildebank kjenner til

Når du oppretter en ny bildesamling med `create`, lagres den automatisk i denne
oversikten. Hvis du allerede hadde en bildesamling fra før, blir den lagt til
neste gang du bruker Bildebank med den bildesamlingen.

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

Bruk samme navn som du brukte da du importerte. Ikke bruk stien til mappen,
USB-disken eller CD-en.

Med `--dry-run` kontrollerer Bildebank filene og viser hva som ville blitt
fjernet, uten å endre databasen eller slette filer.

Kjør uten `--dry-run` når du er sikker:

```powershell
bildebank unimport --name "TestBilder"
```

`unimport` er en kraftig kommando, fordi den kan fjerne filer fra den aktive
bildesamlingen. Bruk den bare når du er sikker på at du har valgt riktig kilde.

Før Bildebank endrer noe, kontrollerer programmet at alle filene fra denne
kilden fortsatt finnes i kilden, og at de er identiske med det som ble
importert. Hvis en fil mangler eller er endret, stopper programmet uten å gjøre
endringer. Grunnen er at du skal kunne importere samme kilde på nytt senere.

Hvis et bilde også finnes i andre kilder, blir bildet liggende i
bildesamlingsmappen. Da fjernes bare koblingen til kilden du angrer. Hvis
bildet bare kom fra denne ene kilden, fjernes det fra den aktive
bildesamlingen.

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

Når du angrer en navngitt import, fjerner Bildebank også denne kilden fra
kildelisten.

## Finn navnekollisjoner

En navnekollisjon betyr at flere importerte filer ville hatt samme filnavn i
samme mappe for en bestemt måned og år. Bildebank beholder filene, men lagrer
noen av dem med justert navn.

Dette er vanligvis ikke et problem, men det kan være nyttig å undersøke hvis du
feilsøker, eller hvis du tror at samme bilde finnes i forskjellig oppløsning fra
to kilder med samme filnavn.

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

Hvis du vil fjerne en importert fil fra den aktive bildebanken, bruker du
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

Se også [`remove`](remove.md).

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
ber om det etter `bildebank update`, går du til bildesamlingsmappen og kjører:

```powershell
cd $HOME\BildeSamling
bildebank migrate
```

Migrering til gjeldende databaseformat gjelder bare brukere som har opprettet
bildesamlingsdatabasen med en eldre versjon av Bildebank. Nye databaser
opprettet med denne versjonen av Bildebank bruker riktig format allerede og
trenger ikke denne migreringen.

Du kan kontrollere hva migreringen vil gjøre uten å endre databasen:

```powershell
bildebank migrate --check
```

Når `bildebank migrate` kjøres, lager programmet en backup av `.bilder.sqlite3`
før databasen endres. Hvis migreringen feiler, skal databasen ikke oppgraderes,
og backupen beholdes.

## Sikkerhet og backup

Bildebank er ikke en backup-løsning. Programmet organiserer og kopierer bilder
og videoer inn i en ny samling, men det erstatter ikke sikkerhetskopier.

Ikke slett originalkilder etter import bare fordi Bildebank har kopiert filene.
Kontroller først at importen ser riktig ut, at `index.html` viser det du
forventer, og at bildesamlingen er sikkerhetskopiert til mer enn ett trygt sted.

En enkel regel er 3-2-1-regelen: ha minst 3 kopier av viktige filer, på minst
2 forskjellige lagringsmedier, og minst 1 kopi et annet sted enn hjemme.

Behold gamle kilder til du er sikker på at den nye samlingen er kontrollert og
sikkerhetskopiert.

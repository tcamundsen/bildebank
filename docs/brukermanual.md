# Brukermanual for Bildebank

Denne manualen er for deg som bruker Windows og PowerShell, og som allerede
har fulgt `README.md` og installert Bildebank.

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

Bildesamlingsmappen skal ikke ligge inni programmappen. Hold programmet og
bildesamlingen adskilt.

Bildesamlingsmappen er en mappe Bildebank styrer. Ikke flytt, gi nytt navn til
eller slett filer inne i denne mappen manuelt, med mindre manualen sier det.
Bruk Bildebank-kommandoer som `remove` når du vil fjerne noe fra samlingen.

Du kan åpne og se på filene i bildesamlingsmappen, men ikke rydd manuelt i
årsmappene og månedsmappene. Da kan databasen og filene komme ut av sync.

## Opprett en testmappe med noen få bilder

Ikke start med hele hovedsamlingen første gang. Lag heller en mappe du gir
navnet TestBilder i Bilder-mappen som du finner med Filutforsker i Windows.
Kopier inn noen få bilder dit.
Da kan du kontrollere at importen fungerer før du
bruker Bildebank på større mengder.

Trykk Windows-tast+E for å åpne Filutforsker. Du kan enten bla deg frem til
Bilder-mappen, eller klikke i adressefeltet øverst i vinduet, skrive `Bilder`
og trykke Enter.


## Åpne PowerShell og gå til riktig mappe

Åpne PowerShell slik:

1. Trykk på Start-menyen i Windows.
2. Skriv `PowerShell`.
3. Åpne `Windows PowerShell`.

Når du skriver kommandoer, er det viktig hvilken mappe PowerShell står i. Du
ser ofte gjeldende mappe helt til venstre på linjen.

Sjekk at Bildebank starter:

```powershell
bildebank
```

Programmet vil da skrive ut en liste over kommandoer som kan kjøres.

Hvis bildesamlingsmappen ikke finnes ennå, kan du lage den først:

```powershell
mkdir $HOME\BildeSamling
```

Når du skal jobbe med bildesamlingen, går du til bildesamlingsmappen:

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

## Legg til en kildemappe

En kildemappe er en mappe der du allerede har bilder eller videoer som skal
importeres.

Legg til testmappen:

```powershell
bildebank add "$HOME\Pictures\TestBilder"
```

For en annen mappe bruker du samme mønster:

```powershell
bildebank add "sti\til\bilder"
```

eller

```powershell
bildebank add "\Users\Tom\Julen2022"
```

Bruk hermetegn rundt stier. Det er spesielt viktig hvis mappenavnet inneholder
mellomrom.

## Tørrtest importen

Før du importerer på ordentlig, kan du kjøre en tørrtest:

```powershell
bildebank import --dry-run
```

Da viser Bildebank hva programmet ville importert, uten å kopiere filer og uten
å endre databasen.

Se gjennom listen. Hvis den ser riktig ut, kan du importere på ordentlig.

## Importer

Kjør import slik:

```powershell
bildebank import
```

Bildebank kopierer støttede bilder og videoer inn i bildesamlingsmappen og
plasserer dem etter dato, for eksempel i mapper som `2024\01`.

På slutten skriver programmet en oppsummering, for eksempel:

```text
Oppsummering: scannet=10, importert=10, duplikater=0, eksisterende=0, dekket=0, navnekollisjoner=0, feil=0
```

`import` importerer bare kilder som er nye eller ikke ferdig importert. Hvis du
kjører `import` en gang til uten å ha lagt til noe nytt, er det normalt å få
`scannet=0`. Det betyr vanligvis bare at det ikke var noe nytt å gjøre.

Hvis du vil importere en ny vanlig mappe senere, kjør først `add` på den nye
mappen og deretter `import` igjen.

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

## Import fra CD, USB og flyttbare medier

For CD-er, USB-disker, minnekort og andre flyttbare medier bruker du
`import-removable`. Ikke kjør `add` først for slike medier.

Gi mediet et stabilt navn med `--name`. Bruk for eksempel teksten som står på
CD-en, navnet på USB-disken, eller et annet navn du vil kjenne igjen senere.

Tørrtest først:

```powershell
bildebank import-removable --name "Familie-CD-2004" --dry-run E:\
```

Importer på ordentlig:

```powershell
bildebank import-removable --name "Familie-CD-2004" E:\
```

Bytt ut `E:\` med stasjonen eller mappen der mediet finnes hos deg. Grunnen til
at `--name` er viktig, er at samme stasjonsbokstav kan brukes av forskjellige
CD-er og USB-disker på forskjellige tidspunkt.

## Se registrerte kilder

Kjør:

```powershell
bildebank list-sources
```

Listen viser kildene Bildebank kjenner til. Vanlige mapper vises som
`directory`, og flyttbare medier vises som `removable`.

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

Eksempel:

```powershell
bildebank unimport "$HOME\Pictures\TestBilder"
```

Bytt ut stien med kilden du vil angre. Hvis importen ble gjort med
`import-removable`, skal du bruke navnet du ga med `--name`, ikke stien til
USB-disken eller CD-en:

```powershell
bildebank unimport --name "Familie-CD-2004"
```

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
Kilde: C:\Users\Tom\Pictures\TestBilder
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

Når du angrer en import som ble gjort med `import-removable`, fjerner Bildebank
også denne kilden fra kildelisten. Du trenger derfor ikke kjøre `remove-source`
etterpå for CD-er, USB-disker og andre flyttbare medier.

## Fjerne en kilde fra kildelisten

Hvis du har lagt til feil kilde med `add`, men ikke importert den ennå, kan du
fjerne den fra kildelisten:

```powershell
bildebank remove-source "$HOME\Pictures\TestBilder"
```

Den samme kommandoen kan også brukes etter at du først har kjørt `unimport` på
en kilde.

For en kilde som ble importert med `import-removable`, bruk navnet:

```powershell
bildebank remove-source --name "Familie-CD-2004"
```

`remove-source` sletter ikke bilder. Den fjerner bare kilden fra listen over
kilder Bildebank kjenner til.

Hvis en vanlig kildemappe fortsatt har importerte filer i samlingen, nekter
Bildebank å fjerne den. Da må du først angre importen:

```powershell
bildebank unimport "$HOME\Pictures\TestBilder"
```

og deretter fjerne kilden fra listen:

```powershell
bildebank remove-source "$HOME\Pictures\TestBilder"
```

For flyttbare medier gjør `unimport --name` begge deler: den angrer importen og
fjerner kilden fra kildelisten.

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

Se detaljer for en bestemt importert målfil:

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
viser Bildebank også hvilken original kildefil den importerte målfilen kom fra.

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

Migrering til databaseformat v3 gjelder bare brukere som har opprettet
bildesamlingsdatabasen med en versjon av Bildebank fra før 7. mai 2026. Nye
databaser opprettet med Bildebank fra og med 7. mai 2026 bruker v3-formatet
allerede og trenger ikke denne migreringen.

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

# Brukermanual for Bildebank

Denne manualen er for deg som bruker Windows og PowerShell, og som allerede
har fulgt `README.md` og installert Bildebank.

Eksemplene under bruker disse mappene:

- programmappen: `$HOME\kode\bildebank`
- bildesamlingsmappen: `$HOME\BildeSamling`
- en liten testkilde: `$HOME\TestBilder`

Bytt ut mappenavnene hvis du har valgt andre steder.

## Programmappen og bildesamlingsmappen

Bildebank bruker to forskjellige typer mapper.

**Programmappen** er mappen der selve Bildebank-programmet ligger. Hvis du har
fulgt `README.md`, heter den ofte:

```powershell
$HOME\kode\bildebank
```

I programmappen ligger filer som `README.md`, `setup-windows.ps1`,
`update.ps1`, `bin\bildebank.cmd` og Python-miljøet `.venv`.

**Bildesamlingsmappen** er mappen der din nye bildebank skal ligge. Det er her
Bildebank lager databasen, årsmappene, månedsmappene og `index.html`.

Eksempel:

```powershell
$HOME\BildeSamling
```

Bildesamlingsmappen skal ikke ligge inni programmappen. Hold programmet og
bildesamlingen adskilt.

## Åpne PowerShell og gå til riktig mappe

Åpne PowerShell slik:

1. Trykk på Start-menyen i Windows.
2. Skriv `PowerShell`.
3. Åpne `Windows PowerShell`.

Når du skriver kommandoer, er det viktig hvilken mappe PowerShell står i. Du
ser ofte gjeldende mappe helt til venstre på linjen.

Gå til programmappen slik:

```powershell
cd $HOME\kode\bildebank
```

Sjekk at Bildebank starter:

```powershell
bildebank --help
```

Når du skal jobbe med bildesamlingen, gå til bildesamlingsmappen:

```powershell
cd $HOME\BildeSamling
```

Hvis mappen ikke finnes ennå, kan du lage den først:

```powershell
mkdir $HOME\BildeSamling
cd $HOME\BildeSamling
```

Når du står i bildesamlingsmappen, kan du kjøre Bildebank direkte:

```powershell
bildebank --help
```

## Første test med en liten bildemappe

Ikke start med hele hovedsamlingen første gang. Lag heller en liten mappe med
noen få bilder og videoer. Da kan du kontrollere at importen fungerer før du
bruker Bildebank på større mengder.

Eksempel:

```powershell
mkdir $HOME\TestBilder
```

Kopier noen få testbilder inn i `TestBilder` med Filutforsker.

Lag deretter bildesamlingsmappen:

```powershell
mkdir $HOME\BildeSamling
cd $HOME\BildeSamling
```

## Opprett målmappe: `bildebank create .`

Kommandoen `create` gjør den mappen du står i til en Bildebank-målmappe. Punktum
betyr "denne mappen".

Kjør dette fra bildesamlingsmappen:

```powershell
bildebank create .
```

Bildebank oppretter databasen sin i bildesamlingsmappen. Etterpå er dette
mappen du vanligvis står i når du bruker Bildebank.

## Legg til en kildemappe: `bildebank add "sti\til\bilder"`

En kildemappe er en mappe der du allerede har bilder eller videoer som skal
importeres.

Legg til testmappen:

```powershell
bildebank add "$HOME\TestBilder"
```

For en annen mappe bruker du samme mønster:

```powershell
bildebank add "sti\til\bilder"
```

Bruk hermetegn rundt stier. Det er spesielt viktig hvis mappenavnet inneholder
mellomrom.

## Tørrtest importen: `bildebank import --dry-run`

Før du importerer på ordentlig, kan du kjøre en tørrtest:

```powershell
bildebank import --dry-run
```

Da viser Bildebank hva programmet ville importert, uten å kopiere filer og uten
å endre databasen.

Se gjennom listen. Hvis den ser riktig ut, kan du importere på ordentlig.

## Importer: `bildebank import`

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

## Se status: `bildebank status`

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

## Lag HTML-visning: `bildebank browser-browser`

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

Browseren åpnes ved å dobbeltklikke på `index.html` med filutforskeren
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

## Se registrerte kilder: `list-sources`

Kjør:

```powershell
bildebank list-sources
```

Listen viser kildene Bildebank kjenner til. Vanlige mapper vises som
`directory`, og flyttbare medier vises som `removable`.

Dette er nyttig når du vil kontrollere hva som allerede er registrert og om en
kilde er importert.

## Finn navnekollisjoner

En navnekollisjon betyr at flere importerte filer ville hatt samme filnavn i
samme målmappe. Bildebank beholder filene, men lagrer noen av dem med justert
navn.

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

## Finn filer uten metadatodato

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

# Lisens

Bildebank er fri programvare lisensiert under GNU General Public License,
versjon 3 eller senere. Se `LICENSE` for full lisens.

# Installasjon

Denne oppskriften er skrevet for Windows 11 og for deg som ikke vanligvis
bruker Git eller Python. Målet er å laste ned programmet fra GitHub, kjøre det
fra en lokal mappe, og senere kunne hente oppdateringer med `git pull`.

**Foreløpig er ikke programmet klart til å slippes løs på slekta.
Jeg skal si fra når det er klart.**

## Kortversjon

Du trenger:

1. Git for Windows
2. Python 3.13 eller nyere
3. En lokal kopi av programmet fra GitHub
4. En Python-venv i programmappen

Selve bildesamlingen bør ligge i en egen mappe utenfor programmappen.

## Anbefalt installasjon

Åpne setup-scriptet her:

[setup-windows.ps1](https://github.com/tcamundsen/bildebank/blob/main/setup-windows.ps1)

Klikk på nedlastingsknappen på GitHub-siden, eller klikk `Raw` og lagre siden
som `setup-windows.ps1`. Høyreklikk på den nedlastede filen og velg
`Run with PowerShell`.

Hvis repoet fortsatt er privat, må du være innlogget på GitHub og ha tilgang
til repoet for at lenken skal fungere.

Scriptet forsøker å:

- installere Git for Windows hvis Git mangler
- installere Python 3.13 hvis Python 3.13 mangler
- laste ned programmet fra GitHub
- lage Python-miljøet `.venv`
- installere programmet i Python-miljøet
- legge `bin`-mappen i brukerens `PATH`

Når scriptet er ferdig, lukk PowerShell og åpne PowerShell på nytt. Da skal du
kunne skrive:

```powershell
bildebank --help
```

Hvis scriptet ikke får installert Git eller Python automatisk, kan du følge den
manuelle oppskriften under.

## Installer Git for Windows

1. Gå til <https://git-scm.com/download/win>
2. Last ned Git for Windows.
3. Kjør installasjonsprogrammet.
4. Bruk standardvalgene hvis du er usikker.

Etterpå åpner du PowerShell og sjekker at Git virker:

```powershell
git --version
```

Hvis du får et versjonsnummer, er Git installert.

## Installer Python

1. Gå til <https://www.python.org/downloads/windows/>
2. Last ned Python 3.13 eller nyere.
3. Start installasjonsprogrammet.
4. Huk av for `Add python.exe to PATH` hvis valget vises.
5. Velg vanlig installasjon.

Sjekk etterpå i PowerShell:

```powershell
py --version
```

Hvis du får et versjonsnummer på 3.13 eller nyere, er Python klar.

## Last ned programmet fra GitHub

Velg først en mappe der du vil ha selve programkoden. Eksempelet under lager
en mappe `kode` under hjemmemappen din:

```powershell
mkdir $HOME\kode
cd $HOME\kode
```

Last ned programmet:

```powershell
git clone https://github.com/tcamundsen/bildebank.git
cd bildebank
```

Hvis repoet senere får et annet navn eller en annen adresse, bruker du den
adressen i stedet.

## Lag Python-miljø for programmet

Kjør disse kommandoene fra programmappen `bildebank`:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Hvis du har installert Python 3.14, kan du også bruke:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Sjekk at programmet starter:

```powershell
.\bin\bildebank.cmd --help
```

Det finnes også et PowerShell-script:

```powershell
.\bin\bildebank.ps1 --help
```

Hvis PowerShell nekter å kjøre `bildebank.ps1`, kjør dette én gang:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Lukk PowerShell, åpne PowerShell på nytt, gå tilbake til programmappen og prøv
igjen:

```powershell
cd $HOME\kode\bildebank
.\bin\bildebank.ps1 --help
```

## Opprett en bildesamling

Lag en egen mappe for den nye samlingen. Den skal ikke ligge inni
programmappen.

Eksempel:

```powershell
mkdir $HOME\BildeSamling
cd $HOME\BildeSamling
```

Opprett målmappe og database:

```powershell
..\kode\bildebank\bin\bildebank.cmd target .
```

Nå kan du legge til en kildemappe med bilder og videoer:

```powershell
..\kode\bildebank\bin\bildebank.cmd add "sti\til\kildemappe"
```

Bytt ut `sti\til\kildemappe` med mappen der bildene ligger.

Importer:

```powershell
..\kode\bildebank\bin\bildebank.cmd import
```

Se status:

```powershell
..\kode\bildebank\bin\bildebank.cmd status
```

Lag HTML-browser:

```powershell
..\kode\bildebank\bin\bildebank.cmd export-html
```

Etterpå kan du åpne `index.html` i bildesamlingsmappen.

## Anbefalt første test

Ikke start med hele hovedsamlingen første gang. Lag heller en liten testmappe
med noen få bilder og videoer, og importer den først. Da ser du at programmet
plasserer filene slik du forventer før du kjører en større import.

Du kan også kjøre en tørrtest før import:

```powershell
..\kode\bildebank\bin\bildebank.cmd import --dry-run
```

Da viser programmet hva det ville gjort, uten å kopiere filer eller endre
databasen.

## Hente oppdateringer

Når det kommer en ny versjon av programmet, gå til programmappen og kjør:

```powershell
cd $HOME\kode\bildebank
.\update.ps1
```

Hvis `update.ps1` ikke virker, kan du gjøre det manuelt:

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install -e .
```

Deretter kan du bruke programmet som før.

## Vanlige problemer

### `git` finnes ikke

Git er ikke installert, eller PowerShell ble åpnet før Git ble installert.
Installer Git for Windows og åpne PowerShell på nytt.

### `py` finnes ikke

Python er ikke installert, eller Python ble installert uten å bli lagt i PATH.
Installer Python på nytt og huk av for `Add python.exe to PATH` hvis valget
vises.

### PowerShell nekter å kjøre `bildebank.ps1`

Kjør:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Åpne PowerShell på nytt etterpå.

### `bildebank.exe` virker ikke

Ikke bruk `bildebank.exe` direkte. Bruk `bildebank.cmd`:

```powershell
.\bin\bildebank.cmd --help
```

Eller, hvis du står i en bildesamlingsmappe:

```powershell
..\kode\bildebank\bin\bildebank.cmd --help
```

### Programmet finner ikke kildemappen

Sjekk at stien er riktig. Bruk hermetegn rundt stier med mellomrom:

```powershell
..\kode\bildebank\bin\bildebank.cmd add "sti med mellomrom\bilder"
```

Unngå å avslutte stien med `\` rett før avsluttende hermetegn. Skriv heller:

```powershell
..\kode\bildebank\bin\bildebank.cmd add "sti med mellomrom\bilder"
```

ikke:

```powershell
..\kode\bildebank\bin\bildebank.cmd add "sti med mellomrom\bilder\"
```

## Viktig om sikkerhet og backup

Programmet skal samle og organisere bilder og videoer, men det er ikke en
backup-løsning. Når du har fått en ryddig bildesamling, bør den sikkerhetskopieres
grundig til mer enn ett sted.

Ikke slett gamle kilder før du er trygg på at importen ble riktig og at den nye
samlingen er sikkerhetskopiert.

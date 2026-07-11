# Bildebank på Linux/WSL

Kort oppskrift for deg som vil installere og kjøre Bildebank fra Linux eller WSL.

## Viktig om WSL

Fra WSL kan Bildebank bare brukes med en bildesamling som ligger på WSLs eget
Linux-filsystem. Bildebank stopper hvis bildesamlingen ligger på en
Windows-disk som er montert i WSL. Kjør i så fall Bildebank direkte i Windows.

Dette gjelder plasseringen av selve bildesamlingen. Kildemapper som bare leses
under import kan fortsatt ligge på en Windows-disk.

## Krav

Du trenger Git og Python 3.13 eller nyere.

På Debian/Ubuntu/WSL kan det typisk installeres slik:

```bash
sudo apt update
sudo apt install git python3 python3-venv
python3 --version
```

Hvis `python3 --version` viser eldre enn 3.13, må du installere Python 3.13
eller nyere før du fortsetter. Hvis du er usikker på hvordan, er dette et godt
sted å be om hjelp.

Hvis du skal bruke ansiktsgjenkjenning med InsightFace i Linux/WSL, trenger
OpenCV også systembiblioteket `libGL`:

```bash
sudo apt install libgl1
```

## Installer programmet

Velg en mappe for programkoden, klon repoet og installer Bildebank i en lokal `.venv`:

```bash
mkdir -p ~/kode
cd ~/kode
git clone https://github.com/tcamundsen/bildebank.git
cd bildebank
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

## Installer InsightFace (valgfritt)

Hvis du vil bruke ansiktsgjenkjenning, installer InsightFace-tillegget fra
programmappen:

```bash
./install-insightface.sh
```

Slå deretter på ansiktsgjenkjenning og kontroller installasjonen:

```bash
bildebank config face_recognition enable
bildebank doctor
```

## Kjør Bildebank

Du kan alltid kjøre programmet med Python fra `.venv`:

```bash
cd ~/kode/bildebank
./.venv/bin/python -m bildebank --help
```

For å kunne skrive bare `bildebank`, legg programmets `.venv/bin` i `PATH`:

```bash
export PATH="$HOME/kode/bildebank/.venv/bin:$PATH"
bildebank --help
```

Denne linjen gjelder bare i terminalvinduet du står i nå. Legg samme
`export`-linje i `~/.bashrc` hvis den skal gjelde i nye terminaler også:

```bash
echo 'export PATH="$HOME/kode/bildebank/.venv/bin:$PATH"' >> ~/.bashrc
```

Neste gang du åpner terminalen, skal `bildebank --help` virke uten den lange
stien.

## Eksempel

Opprett en bildesamling og importer en mappe:

```bash
mkdir -p ~/bilder/samling
cd ~/bilder/samling
bildebank create .
bildebank import --name "Pictures" --dry-run ~/Pictures
bildebank import --name "Pictures" ~/Pictures
```

## Oppdater

Når programmet er installert, kan du hente siste versjon slik:

```bash
bildebank update
```

Hvis `bildebank` ikke ligger i `PATH`, bruk den lange kommandoen:

```bash
cd ~/kode/bildebank
./.venv/bin/python -m bildebank update
```

Hvis det heller ikke virker, kan du gjøre det samme manuelt:

```bash
cd ~/kode/bildebank
git pull --ff-only
./.venv/bin/python -m pip install -e .
```

# Avhengigheter

Dette dokumentet oppsummerer hvilke avhengigheter Bildebank har, hva som er
obligatorisk, og hva som er valgfrie tillegg. Målet er at `run-server` etter
hvert kan vise status for valgfrie funksjoner: installert, tilgjengelig og
aktivert.

## Basisinstallasjon

Basisinstallasjonen gjøres av `setup-windows.ps1`, som lager `.venv` og kjører:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Dette installerer avhengighetene i `pyproject.toml`.

### Python

- Obligatorisk.
- `pyproject.toml` krever Python `>=3.13`.
- `setup-windows.ps1` installerer/bruker Python 3.13 via `py -3.13`.
- Windows-oppsettet er hovedmål for brukere. WSL/Linux brukes primært under
  utvikling og testing.

### Pillow

- Obligatorisk Python-avhengighet i `pyproject.toml`.
- Installeres automatisk av `setup-windows.ps1`.
- Brukes av:
  - `make-thumbnails` til å åpne bilder, bruke EXIF-rotasjon og lage
    thumbnails.
  - OpenCLIP-koden til å åpne bilder, bruke EXIF-rotasjon og konvertere til
    RGB før embedding.
  - tester som lager små testbilder.

### h3

- Obligatorisk Python-avhengighet i `pyproject.toml`.
- Installeres automatisk av `setup-windows.ps1`.
- Brukes til geografisk gruppering:
  - beregne H3-celler fra GPS-koordinater
  - validere H3-celler
  - finne parent/child-celler
  - finne naboer til `/geo/map`
  - tegne H3-klynger i `run-server`

## Valgfrie Python-tillegg

Valgfrie tillegg ligger som extras i `pyproject.toml`. De installeres ikke av
vanlig `setup-windows.ps1`.

### InsightFace

- Brukes til ansiktsgjenkjenning.
- Installeres med:

```powershell
.\install-insightface.ps1
```

Scriptet kjører:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[face]
```

`face` extra inneholder:

- `insightface==1.0.1`
- `numpy`
- `onnxruntime`

Koden importerer også `cv2` i ansiktsskanningen. Dette kommer normalt som en
transitiv avhengighet via InsightFace-installasjonen, men hvis dette endrer seg
må `opencv-python` vurderes som eksplisitt dependency.

Funksjonen er i tillegg styrt av config:

```toml
[face_recognition]
enabled = true
```

Status vises i dag av `bildebank doctor` og på `/app` i `run-server`.

### OpenCLIP

- Brukes til tekstbasert bildesøk.
- Installeres med:

```powershell
.\install-openclip.ps1
```

Scriptet kjører:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[openclip]
```

`openclip` extra inneholder:

- `open_clip_torch`

Denne pakken trekker inn større ML-avhengigheter, blant annet PyTorch. Koden
sjekker også eksplisitt etter `torch`.

`install-openclip.ps1` laster ned og tester disse modellene til
`.bildebank-openclip`:

- `ViT-B-32` med `laion2b_s34b_b79k`
- `ViT-L-14` med `laion2b_s32b_b82k`

OpenCLIP kan bruke nett ved første modellnedlasting. Skanning og søk skal
deretter kjøre lokalt.

Status vises i dag av `bildebank image-status` og på `/app` i `run-server`.

## Eksterne programmer

Dette er ikke Python-biblioteker i `.venv`, men separate programmer som kan
være nødvendige for noen funksjoner.

### Git

- Trengs av `setup-windows.ps1` for å klone/oppdatere repoet.
- Installeres av `setup-windows.ps1` via winget hvis det mangler.

### ExifTool

- Brukes av:
  - `geo-scan` til å lese GPS-metadata.
  - `exiftool-metadata-gaps` til å finne metadata-datoer som Bildebank ikke
    leser selv ennå.
- Installeres av `setup-windows.ps1` til `bildebank-tools\exiftool` i
  programmappen.
- Kan repareres eller installeres på nytt med `bildebank exiftool-install`.
- Bildebank bruker managed ExifTool først, og faller tilbake til `PATH` hvis
  managed ExifTool ikke finnes.
- Brukeren kan angi sti eksplisitt som nødventil, for eksempel:

```powershell
bildebank geo-scan --exiftool "C:\Tools\exiftool.exe"
```

### FFmpeg og FFprobe

- Brukes av `make-video-previews` til å lese AVI-strømmer og lage
  nettleserkompatible MP4-filer med H.264 (`libx264`) og AAC.
- Windows-installasjonen bruker den fastlåste GyanD essentials-byggingen
  `8.1.2` fra GitHub. Arkivet verifiseres mot SHA-256
  `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec`.
- Installeres versjonert under `bildebank-tools\ffmpeg\8.1.2` av både
  `setup-windows.ps1` og `update.ps1`. Launcheren prøver også installasjon når
  programmet mangler, slik at en eldre installasjon får avhengigheten etter en
  vanlig oppdatering.
- Installasjonsfeil skal ikke rulle tilbake eller blokkere en ellers vellykket
  Bildebank-oppdatering. Launcheren prøver igjen ved neste oppstart.
- Bildebank foretrekker den administrerte installasjonen og faller tilbake til
  et komplett `ffmpeg`/`ffprobe`-par i `PATH`.
- `ffmpeg-install --force` reparerer eller erstatter den administrerte
  installasjonen atomisk etter validering.

## Nåværende statusvisning

`run-server` sin `/app`-side viser per nå:

- Bildebank-versjon
- InsightFace aktivert/installert
- OpenCLIP tilgjengelig
- OpenCLIP-modell, pretrained og device

Det kan være nyttig å utvide denne siden med tydeligere skille mellom:

- installert Python-pakke
- aktivert i config
- nødvendige modellfiler finnes
- eksternt program finnes, for eksempel ExifTool

# Avhengigheter

Dette dokumentet oppsummerer hvilke avhengigheter Bildebank har, hva som er
obligatorisk, og hva som er valgfrie tillegg. Målet er at `run-server` etter
hvert kan vise status for valgfrie funksjoner: installert, tilgjengelig og
aktivert.

## Basisinstallasjon

Basisinstallasjonen gjøres av `setup-windows.ps1`, som lager `.venv`, installerer
den komplette Windows-låsen med pips hashkontroll og installerer Bildebank-koden
separat:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r .\requirements\windows-py313-base.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
```

Dette installerer avhengighetene i `pyproject.toml`.

### Python

- Obligatorisk.
- `pyproject.toml` krever Python `>=3.13,<3.14`.
- `setup-windows.ps1` installerer/bruker Python 3.13 via `py -3.13`.
- Windows-oppsettet er hovedmål for brukere. WSL/Linux brukes primært under
  utvikling og testing.
- Se [dependency-locks.md](dependency-locks.md) når pakkeversjoner skal
  oppdateres og låses på nytt.

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

Scriptet installerer den komplette InsightFace-låsen:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r .\requirements\windows-py313-face.lock
```

`face` extra inneholder:

- `insightface==1.0.1`
- `numpy`
- `onnxruntime`

Modellpakkene `antelopev2` og `buffalo_l` lastes fra InsightFaces v0.7-release.
Bildebank har fast URL, arkivstørrelse, SHA-256 og forventede ONNX-filer for
begge. Arkivet kontrolleres før utpakking. Bare de forventede filene trekkes ut
til staging, og modellen publiseres først når alle filnavn og størrelser
stemmer.

En komplett eksisterende modell med forventede filnavn og størrelser brukes
uten ny nedlasting. En ikke-tom modellmappe som er ufullstendig eller avvikende,
beholdes uendret og gir feil. Den erstattes ikke automatisk fordi eksisterende
face-databaser kan inneholde embeddings fra akkurat disse modellfilene.
Andre modellnavn kan fortsatt brukes når modellfilene er installert manuelt,
men Bildebank laster dem ikke ned uten en fast SHA-256.

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

Scriptet installerer den komplette OpenCLIP-låsen:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r .\requirements\windows-py313-openclip.lock
```

`openclip` extra inneholder:

- `open_clip_torch`

Denne pakken trekker inn større ML-avhengigheter, blant annet PyTorch. Koden
sjekker også eksplisitt etter `torch`.

`install-openclip.ps1` laster ned og tester disse modellene til
`.bildebank-openclip`:

- `ViT-B-32` med `laion2b_s34b_b79k`
- `ViT-L-14` med `laion2b_s32b_b82k`

Begge modellene er safetensors-filer med fast Hugging Face-repository,
repository-revisjon, filnavn, størrelse og SHA-256 i
`bildebank/openclip_models.py`. Nedlasting skjer til en unik stagingmappe med
størrelsesgrense og eksklusiv filoppretting. Modellen publiseres først etter
størrelse- og hashkontroll.

En eksisterende cache fra OpenCLIP/Hugging Face brukes direkte når den ligger
under modellroten og svarer til eksakt fastlåst revisjon, filnavn, størrelse og
SHA-256. Den kopieres ikke, slik at store modellfiler ikke dupliseres.

`image-scan`, `image-search` og serverens modellasting får alltid en kontrollert
lokal filsti som `pretrained`. De sender aldri modelltaggen til OpenCLIPs
nedlastingskode. Andre modeller kan bare brukes når `pretrained` er en
eksisterende lokal filsti; Bildebank laster dem ikke ned.

Status vises i launcheren, av `bildebank doctor` og på `/app` i `run-server`.

## Eksterne programmer

Dette er ikke Python-biblioteker i `.venv`, men separate programmer som kan
være nødvendige for noen funksjoner.

### Git

- Trengs av `setup-windows.ps1` for å klone/oppdatere repoet.
- Installeres av `setup-windows.ps1` via winget hvis det mangler.
- `update` krever at Git-sporede filer er uendret, men bevarer og tillater
  usporede filer. Gammel commit lagres i
  `bildebank-tools\\update-pending.txt` før `git pull --ff-only`.
- Etter oppdatering installeres Python-miljøet og en importtest kjøres. Ved
  feil gjenopprettes gammel commit og den gamle Python-installasjonen
  reinstalleres og testes. En gjenværende recovery-markør behandles før neste
  oppdateringsforsøk.

### ExifTool

- Brukes av:
  - `geo-scan` til å lese GPS-metadata.
  - `exiftool-metadata-gaps` til å finne metadata-datoer som Bildebank ikke
    leser selv ennå.
- Installeres av `setup-windows.ps1` til `bildebank-tools\exiftool` i
  programmappen.
- Kan repareres eller installeres på nytt med `bildebank exiftool-install`.
- Windows-installasjonen bruker den fastlåste 64-bit-utgaven `13.58` og
  verifiserer arkivet mot SHA-256
  `fd3b407a01e6ffc6160f2d5fde5ff0c003f6c4c2ba85eee1ce8928ccb51fa3e6`
  før utpakking eller kjøring.
- Arkivet pakkes ut med kontroll av medlemsstier og lenker. Den nye
  installasjonen bygges og versjonskontrolleres i staging før publisering.
  En eksisterende installasjon gjenopprettes ved feil eller kontrollert
  avbrudd under utskifting.
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
  installasjonen atomisk etter validering. Feil og kontrollerte avbrudd under
  publisering gjenoppretter en eksisterende installasjon fra backup hvis
  målmappen mangler.

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

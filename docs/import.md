# import
<!-- CLI-HELP-START -->
```text
usage: bildebank import [valg] --name navn mappe

Registrerer og importerer bildene fra en mappe, USB-brikke, CD eller disk.

positional arguments:
  mappe        Kilden som skal importeres

options:
  -h, --help   show this help message and exit
  --quiet
  --name NAME  Unikt navn på importen, for eksempel Sommer2023 eller Familie-
               CD-2004
  --dry-run    Vis importoppsummering uten å kopiere filer eller endre
               databasen
```
<!-- CLI-HELP-END -->

`import` registrerer en kilde og importerer støttede bilder og videoer derfra
til bildesamlingen. Kilden kan være en mappe, USB-brikke, CD eller disk.

Bildebank kan også importere RAW/NEF og PSD-filer. De brukes i duplikatsjekk og
sortering, men vises ikke som vanlige bilder i nettleseren.

Google/Pixel motion-videoer kan ligge som `.MP`-filer. Når filen faktisk er en
MP4-video, importerer Bildebank den som video og lagrer kopien med `.mp4` i
bildesamlingen. Kildemappen endres ikke.

Bildene kopieres inn i bildesamlingen og plasseres etter dato, for eksempel:

```text
2024\01
2024\02
udatert
```

Bildebank prøver først å finne dato i metadata. Hvis det ikke går, kan programmet
bruke dato fra filens endringstidspunkt eller filnavnet

## Valg

### `--name NAVN`

`--name` gir importen et navn. Navnet må være unikt, og programmet håndhever
dette.

Bruk et navn du kjenner igjen senere. Det samme navnet brukes hvis du vil angre
importen med `unimport`.

Bildebank krever `--name` fordi filstien til importen ikke er en trygg identitet.
I dag kan `E:\` være en CD, og i morgen kan `E:\` være en USB-brikke.

Bruk derfor navn som er lette å kjenne igjen:

```powershell
bildebank import --name "Familie-CD-2004" E:\
bildebank import --name "Minnekort-Kamera-2023-07" F:\
bildebank import --name "GamleBilder-PC" "C:\Users\deg\Pictures\GamleBilder"
```

Du kan ikke gjenbruke samme navn for en ny import. Hvis du importerer flere
deler av samme USB-brikke hver for seg, må hver del få sitt eget navn.

### `--dry-run`

Vis en oppsummering av hva programmet ville gjort med kommandoen din, men uten å
kopiere filer eller endre databasen:

```powershell
bildebank import --name "Sommer2023" --dry-run "C:\Users\deg\Pictures\Sommer2023"
```

Hvis oppsummeringen ser riktig ut, kjører du samme kommando uten `--dry-run`:

```powershell
bildebank import --name "Sommer2023" "C:\Users\deg\Pictures\Sommer2023"
```

## Duplikater

Bildebank unngår å lagre samme bildefil flere ganger. Hvis samme fil finnes i
flere kilder, skal bildet bare ligge ett sted i bildesamlingen. Samtidig husker
Bildebank hvilke kilder bildet kom fra.

Duplikater oppdages med SHA-256-hash. Det betyr at Bildebank kan kjenne igjen
identiske bilder selv om filnavnet er forskjellig i forskjellige importer.

## Overlappende mapper

Det er trygt å importere mapper som overlapper hverandre, så lenge hver import
får sitt eget unike navn.

Eksempel:

```powershell
bildebank import --name "BrikkeA-2023" "F:\Bilder\2023"
bildebank import --name "BrikkeA-hele" F:\
```

I dette eksempelet ligger `F:\Bilder\2023` også inni `F:\`. Når du senere
importerer hele `F:\`, vil Bildebank kjenne igjen bildefilene som allerede er
importert.

De samme bildefilene blir ikke lagret på nytt i bildesamlingen. Bildebank
registrerer bare at de også finnes i importen som heter `BrikkeA-hele`.

Dette gjør det mulig å importere litt og litt først, og eventuelt hele disken
eller USB-brikken senere. Bruk navn som gjør det lett å forstå hva du har gjort:

```powershell
bildebank import --name "BrikkeA-mappe1" "F:\mappe1"
bildebank import --name "BrikkeA-mappe2" "F:\mappe2"
bildebank import --name "BrikkeA-hele" F:\
```

Hvis du etterpå vil rydde bort de første kildehenvisningene, kan du bruke
`unimport` på navnene:

```powershell
bildebank unimport --name "BrikkeA-mappe1"
bildebank unimport --name "BrikkeA-mappe2"
```

Bildene blir liggende hvis de også finnes i `BrikkeA-hele`.

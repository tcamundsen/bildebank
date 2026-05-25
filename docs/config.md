# config

<!-- CLI-HELP-START -->
```text
usage: bildebank config seksjon enable|disable

Slå valgfrie funksjoner på eller av i bildebank-config.toml.
Eksempel:

 bildebank config face_recognition enable
 bildebank config image_search disable

positional arguments:
  seksjon         Config-seksjon som skal endres
  enable|disable  enable slår funksjonen på, disable slår den av

options:
  -h, --help      show this help message and exit
```
<!-- CLI-HELP-END -->

`config` slår valgfrie funksjoner på eller av i `bildebank-config.toml`.
Kommandoen oppretter config-filen hvis den mangler.

Slå på ansiktsgjenkjenning:

```powershell
bildebank config face_recognition enable
```

Slå av ansiktsgjenkjenning:

```powershell
bildebank config face_recognition disable
```

Slå på tekstbasert bildesøk:

```powershell
bildebank config image_search enable
```

Slå av tekstbasert bildesøk:

```powershell
bildebank config image_search disable
```

Dette endrer bare `enabled = true` eller `enabled = false` i den aktuelle
seksjonen. Andre valg, som modellnavn og modellmapper, beholdes.

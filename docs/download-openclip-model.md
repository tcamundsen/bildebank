# download-openclip-model

<!-- CLI-HELP-START -->
```text
usage: bildebank download-openclip-model [valg]

Last ned OpenCLIP-modellen som er valgt i bildebank-config.toml.

options:
  -h, --help       show this help message and exit
  --all-supported  Last ned begge OpenCLIP-modellene som støttes av Bildebank.
```
<!-- CLI-HELP-END -->

Kommandoen laster ned OpenCLIP-modellen som er valgt under `[image_search]` i
`bildebank-config.toml`. Vanligvis trenger du ikke kjøre den selv:
Oppsett-fanen har en egen knapp som laster ned valgt modell.

Bildebank kan automatisk laste ned disse kombinasjonene:

- `ViT-B-32` med `laion2b_s34b_b79k`
- `ViT-L-14` med `laion2b_s32b_b82k`

Modellfilene har fast nedlastingsadresse, repository-revisjon, størrelse og
SHA-256. En modell publiseres ikke i modellmappen før hele filen er kontrollert.
Fremdrift vises i MB og prosent. Hvis nedlastingen avbrytes eller forbindelsen
forsvinner, beholdes den nedlastede delen. Neste kjøring forsøker å fortsette
fra samme sted.

Eksempel:

```powershell
bildebank download-openclip-model
```

Dette laster ned begge de støttede modellene:

```powershell
bildebank download-openclip-model --all-supported
```

En tidligere OpenCLIP-cache brukes videre hvis modellfilen fortsatt svarer
eksakt til den fastlåste modellen. Ukjente eller endrede filer overskrives
ikke.

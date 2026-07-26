# cleanup-thumbnails

<!-- CLI-HELP-START -->
```text
usage: bildebank cleanup-thumbnails [valg]

Vis eller slett gamle thumbnails fra lagringsmåten som ble brukt før
thumbs\v2.

options:
  -h, --help  show this help message and exit
  --apply     Slett gamle thumbnails. Uten dette valget slettes ingenting.
```
<!-- CLI-HELP-END -->

Kommandoen finner gamle miniatyrbilder som ble laget av tidligere versjoner av
Bildebank. De ligger i mapper som:

```text
C:\Bilder\Samling\thumbs\2012\10
C:\Bilder\Samling\thumbs\udatert
```

Bildebank bruker ikke disse filene etter overgangen til `thumbs\v2`, og kommer
ikke til å bruke dem igjen. Det er trygt å slette dem. Originalbildene og de nye
miniatyrbildene under `thumbs\v2` blir ikke berørt.

Når du har laget miniatyrbilder fra Bildebank-vinduet, får du automatisk tilbud
om å slette gamle miniatyrbilder hvis de finnes.

## Kontroll fra PowerShell

Uten `--apply` viser kommandoen bare antall gamle filer og samlet størrelse:

```powershell
bildebank cleanup-thumbnails
```

Ingen filer slettes.

For å slette filene:

```powershell
bildebank cleanup-thumbnails --apply
```

Oppryddingen behandler bare den gamle mappestrukturen. Den følger ikke
symlinker, junctions eller andre Windows reparse points. Uventede filer og
mapper blir liggende.

# image-scan

<!-- CLI-HELP-START -->
```text
usage: bildebank image-scan [valg]

Scan bilder for tekstbasert bildesøk

options:
  -h, --help     show this help message and exit
  --limit LIMIT  Maks antall bildefiler som skal scannes
```
<!-- CLI-HELP-END -->

`image-scan` scanner importerte bilder slik at vi kan kjøre
tekstsøk på innhold i bildene.

Denne må kjøres på nytt hvis du legger til nye bilder. Kommandoen kan avbrytes
med **Ctrl-C**. Den fortsetter da der den slapp neste gang kommandoen kjøres.

```powershell
bildebank image-report
```

Les mer i dokumentet om [OpenClip](openclip.md)

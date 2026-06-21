# export-person

`export-person` kopierer alle bildene som vises på siden til en person i
bildebrowseren. Bekreftede bilder, manuelle koblinger og aktive forslag tas
med. Innstillingen for å skjule «Ute av fokus» brukes også.

Destinasjonsmappen må finnes fra før. Bildebank oppretter en ny undermappe med
personens navn. Denne personmappen kan ikke finnes fra før.

```powershell
bildebank export-person "Tom" --dest "D:\Eksport"
```

Bildene organiseres etter år og måned:

```text
D:\Eksport\Tom\2024\01\IMG_1234.jpg
D:\Eksport\Tom\udatert\gammelt-bilde.jpg
```

Eksporten inneholder også en statisk browser som bare viser de eksporterte
bildene. Åpne den direkte i en nettleser:

```text
D:\Eksport\Tom\index.html
```

Stien øverst i browseren kan brukes til å gå mellom oversikter for år, måneder
og filer.

Bruk `--dry-run` for å kontrollere alle kilde- og målfilene uten å opprette
mapper eller kopiere filer:

```powershell
bildebank export-person "Tom" --dest "D:\Eksport" --dry-run
```

Eksporten bruker en midlertidig mappe ved siden av den ferdige personmappen.
Personmappen får navnet sitt først når alle filer er kopiert og kontrollert.
Hvis kopieringen feiler, beholdes den ufullstendige mappen, og Bildebank viser
hvor den ligger.

## Referanse

<!-- CLI-HELP-START -->
```text
usage: bildebank export-person [valg] navn --dest mappe

Eksporter bildene som vises på personens side i bildebrowseren.

positional arguments:
  navn          Personnavn

options:
  -h, --help    show this help message and exit
  --dest mappe  Eksisterende mappe som personmappen skal opprettes i
  --dry-run     Vis planlagte kopier uten å opprette eller endre noe
```
<!-- CLI-HELP-END -->

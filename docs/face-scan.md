# face-scan

<!-- CLI-HELP-START -->
```text
usage: bildebank face-scan [valg]

Scanner importerte bilder etter ansikter.

options:
  -h, --help            show this help message and exit
  --limit LIMIT         Maks antall bildefiler som skal sjekkes
  --force               Scan valgte bilder på nytt selv om de allerede er
                        scannet
  --discard-confirmed-person-links
                        Tillat at bekreftede ansiktskoblinger for bilder som
                        scannes på nytt blir slettet. Krever --force og
                        nøyaktig bekreftelse.
  --show-model-output   Vis intern output fra InsightFace/ONNX ved feilsøking
```
<!-- CLI-HELP-END -->

`face-scan` scanner importerte bilder og lagrer matematiske beskrivelser av
ansikter i den valgte modellens database under `.bildebank-faces`.

Jobben som `face-scan` gjør kan også gjøres direkte fra Bildebank-vinduet.

Den hopper over bilder som allerede er scannet med samme innhold. Det er trygt
å avbryte med `Ctrl-C`; neste kjøring fortsetter ved å hoppe over det som er
ferdig.

Hvis kommandoen kjøres fra Bildebank-vinduet, kan du bruke knappen
**Avbryt jobb** i stedet for `Ctrl-C`.

Med `--force` scanner den valgte bildene på nytt. Det nye resultatet erstatter
det gamle først når bildet er lest og InsightFace har fullført. Hvis ny
skanning feiler, beholdes tidligere ansikter, forslag og personkoblinger.

Hvis et bilde har bekreftede ansiktskoblinger, nekter `--force` å erstatte
dem. For å gjøre dette med vilje må du også bruke
`--discard-confirmed-person-links` og skrive den eksakte bekreftelsesteksten
som vises. Manuelle koblinger som bare sier at en person finnes i bildet,
beholdes.

Hvis en fil feiler under scanning, skriver `face-scan` filstien og
feilmeldingen. Feilen kan også ses senere med:

```powershell
bildebank face-report
```

## Valg

### `--limit ANTALL`

Skann maksimalt ANTALL bilder. Fint å bruke for å teste på et mindre antall bilder først.

### `--force`

Scan valgte bilder på nytt selv om de allerede er scannet:

```powershell
bildebank face-scan --force --limit 10
```

Hvis ett av de valgte bildene har bekreftede ansiktskoblinger, stopper
kommandoen uten å erstatte dem.

### `--discard-confirmed-person-links`

Tillater at bekreftede ansiktskoblinger erstattes ved tvungen ny skanning:

```powershell
bildebank face-scan --force --discard-confirmed-person-links --limit 10
```

Valget kan bare brukes sammen med `--force`. Bildebank forklarer hva som kan
gå tapt og krever at du skriver en eksakt bekreftelsestekst før skanningen
starter.

### `--show-model-output`

Viser intern output fra InsightFace/ONNX ved feilsøking.

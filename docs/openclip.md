# OpenCLIP

OpenCLIP brukes til tekstbasert bildesøk. Målet er at du skal kunne søke etter
innhold i bilder med vanlige ord, for eksempel:

```powershell
bildebank image-search "strand"
```

OpenCLIP er separat fra ansiktsgjenkjenning. Ansiktsgjenkjenning bruker
InsightFace, mens tekstbasert bildesøk bruker OpenCLIP.

## Status

Dette er en ny funksjon under arbeid.

Første versjon er avgrenset til:

- bilder, ikke video
- søk på hele bildet
- tekstsøk, ikke objektdeteksjon
- modellen `ViT-B-32`
- de 100 beste treffene som standard
- resultatfilen `image-search.html`

Denne siden skal oppdateres etter hvert som OpenCLIP-funksjonen bygges videre.

## Installer OpenCLIP

Kjør dette fra programmappen:

```powershell
.\install-openclip.ps1
```

Scriptet installerer OpenCLIP i Bildebanks lokale Python-miljø og tester modellen:

```text
ViT-B-32 (laion2b_s34b_b79k)
```

Modellfiler lagres lokalt i programmappen:

```text
.bildebank-openclip
```

OpenCLIP kan bruke nettet når modellen lastes ned første gang. Etter at
modellfilene er lastet ned, skal selve scanning og søk kjøre lokalt på maskinen.
Bildene sendes ikke til en nettjeneste for å søkes i.

Hvis du ser en melding om `HF Hub` eller `HF_TOKEN`, betyr det vanligvis at
OpenCLIP henter modellfiler fra Hugging Face uten innlogging. Det er ikke
nødvendig med `HF_TOKEN` for vanlig bruk, men Hugging Face kan gi høyere
nedlastingsgrenser hvis man bruker en token.

## Sjekk status

Du kan se om OpenCLIP er installert med:

```powershell
bildebank face-status
```

Kommandoen viser både ansiktsgjenkjenning og tekstbasert bildesøk.

## Scan bilder

Gå først til bildesamlingen:

```powershell
cd "C:\Users\deg\Pictures\Min bildesamling"
```

Kjør deretter:

```powershell
bildebank image-scan
```

Dette beregner en OpenCLIP-embedding for hvert bilde i samlingen og lagrer den i
en egen database:

```text
.bilder-openclip.sqlite3
```

Du kan teste med noen få bilder først:

```powershell
bildebank image-scan --limit 100
```

Hvis `image-scan` kjøres på nytt, hopper Bildebank over bilder som allerede har
embedding for samme modell og samme filinnhold.

Mens kommandoen kjører, viser den fremdrift i terminalen. Den skriver blant
annet hvor mange bilder som er behandlet, hvor mange som er scannet, hvor mange
som er hoppet over, og om noen bilder feilet.

Det er trygt å avbryte med `Ctrl-C`. `image-scan` lagrer underveis, og neste
gang kommandoen kjøres, hopper Bildebank over bilder som allerede har embedding
for samme modell og samme filinnhold.

## Søk etter bilder

Når bilder er scannet, kan du søke:

```powershell
bildebank image-search "strand"
```

Som standard viser søket de 100 beste treffene og skriver:

```text
image-search.html
```

Kommandoen åpner HTML-filen automatisk. Hvis du bare vil skrive filen uten å
åpne nettleseren:

```powershell
bildebank image-search "strand" --no-browser
```

Du kan også endre antall treff:

```powershell
bildebank image-search "strand" --limit 50
```

Mens søket kjører, viser `image-search` hvor mange bilde-embeddings som er
funnet, og hvor mange bilder søket er sammenlignet med.

## Eksempler på søk

```powershell
bildebank image-search "strand"
bildebank image-search "fjell"
bildebank image-search "dyr"
bildebank image-search "bil"
bildebank image-search "snø"
```

OpenCLIP forstår ikke bildene på samme måte som et menneske. Resultatene er
forslag sortert etter likhet mellom søketeksten og bildet. Noen treff kan derfor
være feil, og noen riktige bilder kan mangle.

## Filer og data

OpenCLIP bruker disse filene:

- `.bildebank-openclip` i programmappen for modellfiler
- `.bilder-openclip.sqlite3` i bildesamlingen for bilde-embeddings og søk
- `image-search.html` i bildesamlingen for siste søkeresultat

Disse er separate fra ansiktsgjenkjenningens filer.

## Foreløpig ikke støttet

- videosøk
- markering av hvor i bildet søketreffet finnes
- automatisk kategorisering av alle bilder
- trening av egen modell
- valg mellom flere OpenCLIP-modeller i brukergrensesnittet

# OpenCLIP

OpenCLIP brukes til tekstbasert bildesøk. Søk kan nå gjøres i nettleseren
etter at du har kjørt [`image-scan`](image-scan.md).

Dette dokumentet har en del teknisk informasjon, samt beskriver søk
fra PowerShell.

Du må bruke engelsk tekst i søkefeltet. Eksempel:

```powershell
bildebank image-search "beach"
```

OpenCLIP er separat fra ansiktsgjenkjenning. Ansiktsgjenkjenning bruker
InsightFace, mens tekstbasert bildesøk bruker OpenCLIP.

## Status

Dette er en ny funksjon som fortsatt er under utvikling.

Første versjon er avgrenset til:

- bilder, ikke video
- søk på hele bildet
- tekstsøk, ikke objektdeteksjon
- modell valgt i config, som standard `ViT-B-32`
- de 100 beste treffene som standard
- resultatfilen `image-search.html`

Denne siden skal oppdateres etter hvert som OpenCLIP-funksjonen bygges videre.

## Installer OpenCLIP

Åpne fanen **Oppsett** i Bildebank-vinduet. Gjør deretter dette i rekkefølge:

1. Trykk **Installer OpenCLIP**.
2. Trykk **Last ned valgt modell**.

Den første knappen installerer programdelene som OpenCLIP trenger. Den andre
knappen laster bare ned modellen som er valgt i innstillingene.

En erfaren bruker kan installere programdelene fra programmappen med:

```powershell
.\install-openclip.ps1
```

Scriptet installerer OpenCLIP i Bildebanks lokale Python-miljø, men laster ikke
ned en modell. Standardmodellen er:

```text
ViT-B-32 (laion2b_s34b_b79k), omtrent 577 MB
```

Den større `ViT-L-14`-modellen er omtrent 1,6 GB. Den lastes bare ned hvis den
er valgt. Søk er tilgjengelig i bildebrowseren. I dette dokumentet vises også
de underliggende kommandoene.

Slå på tekstbasert bildesøk:

```powershell
bildebank config image_search enable
```

Modellfiler lagres lokalt i programmappen:

```text
.bildebank-openclip
```

Bildebank bruker faste repository-revisjoner, filstørrelser og SHA-256 for de
to modellfilene. En tidligere OpenCLIP-cache gjenbrukes bare hvis modellfilen
stemmer med den fastlåste modellen. Ukjente eller endrede filer overskrives
ikke.

Under nedlastingen viser Bildebank mengde og prosent. Du kan bruke **Avbryt
jobb** hvis forbindelsen er dårlig. Den nedlastede delen beholdes, og neste
forsøk fortsetter der det forrige stoppet. Modellen tas ikke i bruk før hele
filen har riktig størrelse og SHA-256.

Nett brukes bare av den eksplisitte installasjonen eller kommandoen
[`download-openclip-model`](download-openclip-model.md). `image-scan`,
`image-search` og nettleserserveren bruker en kontrollert lokal modellfil og
starter aldri en modellnedlasting. Bildene sendes ikke til en nettjeneste.

## Velg modell

OpenCLIP-modellen velges i `bildebank-config.toml`:

```toml
[image_search]
enabled = true
model_root = ".bildebank-openclip"
device = "auto"
model_name = "ViT-B-32"
pretrained = "laion2b_s34b_b79k"
```

For å teste den større L/14-modellen, bytt til:

```toml
[image_search]
enabled = true
model_root = ".bildebank-openclip"
device = "auto"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"
```

Modellen omtales ofte som `ViT-L/14`, men i OpenCLIP-config bruker vi navnet
`ViT-L-14`.

Andre OpenCLIP-modeller lastes ikke ned automatisk. En erfaren bruker kan angi
en eksisterende lokal modellfil som `pretrained`, for eksempel:

```toml
[image_search]
enabled = true
model_root = ".bildebank-openclip"
device = "cpu"
model_name = "ViT-B-32"
pretrained = "C:\\Users\\Tom\\Modeller\\egen-modell.safetensors"
```

Bildebank følger ikke lenker til en slik fil. Brukeren er selv ansvarlig for
opprinnelse og kompatibilitet for egendefinerte modellfiler.

Eldre configfiler kan ha seksjonen `[openclip]`. Bildebank gir den automatisk
nytt navn til `[image_search]` når configfilen leses.

`ViT-L-14` kan gi bedre treff enn `ViT-B-32`, men den er større og tregere.
Når du bytter modell, må image-scan kjøres på nytt for å lage embeddings for
den modellen.  Bildebank lagrer embeddings separat per modell, så du kan bytte
tilbake uten å slette de gamle embeddingene.

Hvis `enabled = false`, skjuler `run-server` bildesøk-knappen og kommandoene
`image-scan` og `image-search` sier fra at tekstbasert bildesøk er slått av.
Eksisterende OpenCLIP-database og embeddings slettes ikke.

Når du åpner **Bildesøk** i `run-server`, begynner serveren å laste
OpenCLIP-modellen i bakgrunnen. Første søk kan fortsatt måtte vente hvis
modellen ikke er ferdig lastet.

## CPU eller GPU

Som standard bruker OpenCLIP:

```toml
device = "auto"
```

Da bruker Bildebank GPU hvis PyTorch finner CUDA. Hvis ikke, brukes CPU. Du kan
tvinge CPU slik:

```toml
device = "cpu"
```

Du kan se hva systemet finner med:

```powershell
bildebank doctor
```

Under `Tekstbasert bildesøk` viser kommandoen blant annet `device-valg`,
om PyTorch er installert, om CUDA/GPU ble funnet, og eventuelt GPU-navn.

## Sjekk status

Du kan se om OpenCLIP er installert med:

```powershell
bildebank doctor
```

Kommandoen viser ansiktsgjenkjenning, tekstbasert bildesøk og ExifTool.

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
annet hvor mange bilder som er behandlet, hvor mange som er skannet, hvor mange
som er hoppet over, om noen bilder feilet, og omtrent hvor lang tid som gjenstår.

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

Treffene i `image-search.html` er sortert med beste match først. Treff nummer
1 er bildet som fikk høyest score fra OpenCLIP.

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
funnet, og hvor mange bilder det er søkt i.

## Hvordan skrive søk

OpenCLIP fungerer best med engelsk tekst. Norske ord kan virke, men forvent
bedre treff med engelsk.

Skriv gjerne søket som en kort beskrivelse av et bilde, ikke bare som et
stikkord:

```powershell
bildebank image-search "a photo of a beach"
bildebank image-search "a child holding a cat"
bildebank image-search "a bride wearing a white wedding dress"
```

Hvis treffene blir for brede, prøv flere varianter:

```powershell
bildebank image-search "wedding dress"
bildebank image-search "a bride in a white dress"
bildebank image-search "a woman wearing a wedding dress"
bildebank image-search "a close-up photo of a white wedding dress"
```

Det kan også hjelpe å søke etter det viktigste eller sjeldneste først. Hvis
`child with a cat` gir mange bilder av bare barn, prøv `cat` eller
`a child holding a cat`.

## Begrensninger i treffsikkerhet

OpenCLIP-søk er ikke et vanlig nøkkelordsøk og ikke en sikker fasit. Bildebank
spør modellen hvilke bilder som ligner mest på søketeksten, og viser de beste
treffene sortert etter score.

Det betyr:

- Søket er ikke et strengt filter.
- `child with a cat` betyr ikke at bildet må inneholde både barn og katt.
- Bilder som bare matcher deler av søket kan komme høyt opp.
- Søk etter klær, detaljer og små objekter kan gi svake treff.
- Hvis samlingen ikke har gode treff, viser Bildebank likevel de beste bildene
  den finner.

Hvis `wedding dress` gir mange tilfeldige bilder, betyr det ikke nødvendigvis at
kommandoen er ødelagt. Det kan bety at modellen ikke skiller godt nok mellom
bildene i samlingen for akkurat det søket, eller at søketeksten bør formuleres
annerledes.

## Eksempler på søk

```powershell
bildebank image-search "a photo of a beach"
bildebank image-search "mountains"
bildebank image-search "animals"
bildebank image-search "a car"
bildebank image-search "snow"
```

OpenCLIP forstår ikke bildene på samme måte som et menneske. Resultatene er
forslag sortert etter likhet mellom søketeksten og bildet. Noen treff kan derfor
være irrelevante, og noen riktige bilder kan mangle.

## Filer og data

OpenCLIP bruker disse filene:

- `.bildebank-openclip` i programmappen for modellfiler
- `.bilder-openclip.sqlite3` i bildesamlingen for bilde-embeddings og søk
- `image-search.html` i bildesamlingen for siste søkeresultat

Disse er separate fra ansiktsgjenkjenningens filer.

Bildebank validerer databasestrukturen før OpenCLIP-data brukes. Eldre,
uversjonerte databaser med komplett kjent struktur merkes automatisk med
gjeldende databaseversjon uten at embeddings beregnes på nytt. Hvis strukturen
er mangelfull eller fra en nyere programversjon, stopper Bildebank uten å
overskrive eksisterende OpenCLIP-data.

## Foreløpig ikke støttet

- videosøk
- markering av hvor i bildet søketreffet finnes
- automatisk kategorisering av alle bilder
- trening av egen modell
- valg mellom flere OpenCLIP-modeller i brukergrensesnittet

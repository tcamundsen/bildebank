# Filtersøk i Bildebank

Filtersøk brukes til å finne bilder og filer ved å kombinere ett eller flere søkekriterier.

Eksempel:

```text
month:12 day:24
```

viser bilder tatt 24. desember, uansett år.

Eksempel:

```text
location:manual h3res>=8
```

viser bilder med manuell H3-plassering der H3-oppløsningen er minst 8.

## Grunnregler

Søkekriterier skrives som ett eller flere uttrykk adskilt med mellomrom.

```text
month:12 day:24
```

Flere kriterier kombineres med **OG**. Det betyr at bildet må oppfylle alle kriteriene for å vises.

Bruk anførselstegn når verdien inneholder mellomrom:

```text
camera:"iPhone 12"
tag:"Ute av fokus"
source:"Mobil 2024"
```

For eksakte verdier kan `:` ofte brukes:

```text
month:12
day:24
location:manual
```

For numeriske filtre kan operatorer brukes:

```text
month>=6 month<=8
width>=3000
size<2MB
```

## Operatorer

Disse operatorene brukes i numeriske filtre:

| Operator | Betydning            |
| -------- | -------------------- |
| `=`      | lik                  |
| `>`      | større enn           |
| `<`      | mindre enn           |
| `>=`     | større enn eller lik |
| `<=`     | mindre enn eller lik |

For `year`, `month` og `day` er `:` det samme som `=`:

```text
year:2024
year=2024
```

```text
month:12
month=12
```

```text
day:24
day=24
```

Noen kombinasjoner er ikke tillatt fordi de er tvetydige. For eksempel:

```text
month=12 month>6
width>100 width>=200
```

Bruk heller én nedre og én øvre grense:

```text
month>=6 month<=8
width>1000 width<4000
```

## Dato

### `year`

Filtrer på år.

```text
year:2024
year=2024
year>2020
year<2025
year>=2020 year<=2024
```

Gyldige verdier er `1` til `9999`.

### `after:YYYY-MM-DD`

Viser bilder med dato etter en bestemt dato.

```text
after:2023-12-01
```

Datoen må skrives på formen `YYYY-MM-DD`.

`after` er en streng sammenligning mot Bildebanks sorteringsdato. I praksis betyr det «etter denne datoen», ikke «fra og med».

### `before:YYYY-MM-DD`

Viser bilder med dato før en bestemt dato.

```text
before:2024-12-12
```

Datoen må skrives på formen `YYYY-MM-DD`.

`before` betyr «før denne datoen», ikke «til og med».

### `month`

Filtrer på måned, uavhengig av år.

```text
month:12
month=12
month>6
month<10
month>=6 month<=8
```

Gyldige verdier er `1` til `12`.

Eksempler:

```text
month:12
```

viser bilder fra desember.

```text
month>=6 month<=8
```

viser bilder fra juni, juli og august.

```text
month>6 month<10
```

viser bilder fra juli, august og september.

### `day`

Filtrer på dag i måneden, uavhengig av måned og år.

```text
day:24
day=24
day>10
day<20
day>=23 day<=25
```

Gyldige verdier er `1` til `31`.

Eksempel:

```text
month=12 day>=23 day<=25
```

viser bilder fra 23., 24. og 25. desember.

### `date`

Filtrer etter hvilken datokilde Bildebank bruker.

```text
date:manual
date:metadata
date:filename
date:mtime
```

Gyldige verdier:

| Verdi      | Betydning                                  |
| ---------- | ------------------------------------------ |
| `manual`   | datoen er satt manuelt i Bildebank         |
| `metadata` | datoen kommer fra metadata                 |
| `filename` | datoen er hentet fra filnavn               |
| `mtime`    | datoen kommer fra filens endringstidspunkt |

Eksempel:

```text
date:manual
```

viser bilder der datoen er satt manuelt.

## Sted

### `location:gps`

Viser bilder med GPS-koordinater fra metadata.

```text
location:gps
```

Bilder med manuell H3-plassering regnes ikke som `location:gps`.

### `location:manual`

Viser bilder som har fått sted satt manuelt med H3.

```text
location:manual
```

### `location:<slug>`

Viser bilder innenfor et definert geografisk sted.

```text
location:kreta
location:narvik
```

Slug er samme tekst som brukes i URL-er for definerte steder, for eksempel `/geo/place/slug`.

### `h3res`

Filtrer på oppløsningen til manuell H3-plassering.

```text
location:manual h3res:11
location:manual h3res=11
location:manual h3res>=8
location:manual h3res<10
```

`h3res` kan bare brukes sammen med:

```text
location:manual
```

Gyldige H3-oppløsninger er `0` til `11`.

## Filtype og filnavn

### `type`

Filtrer etter medietype.

```text
type:image
type:video
type:file
```

| Verdi   | Betydning                                            |
| ------- | ---------------------------------------------------- |
| `image` | bildefiler                                           |
| `video` | videofiler                                           |
| `file`  | støttede filer som ikke regnes som bilde eller video |

### `extension`

Filtrer etter filendelse.

```text
extension:jpg
extension:mp4
```

Punktum kan utelates.

```text
extension:.jpg
extension:jpg
```

begge betyr jpg-filer.

### `filename`

Søk i lagret filnavn.

```text
filename:IMG
filename:"2024 ferie"
```

Søket er et delstrengsøk. Det betyr at `filename:IMG` finner filnavn som inneholder `IMG`.

### `path`

Søk i filens sti i bildesamlingen.

```text
path:2024/01
path:"Sommer 2023"
```

Søket er et delstrengsøk.

## Filstørrelse

### `size`

Filtrer etter filstørrelse.

```text
size>2MB
size>=300KB
size<10MB
size<=2GB
```

Støttede enheter:

| Enhet | Betydning |
| ----- | --------- |
| `B`   | byte      |
| `KB`  | kilobyte  |
| `MB`  | megabyte  |
| `GB`  | gigabyte  |
| `TB`  | terabyte  |

Hvis enhet utelates, tolkes verdien som byte.

Eksempel:

```text
size>2MB
```

viser filer større enn 2 MB.

Eksempel:

```text
size>=300KB size<2MB
```

viser filer fra og med 300 KB og under 2 MB.

`size` støtter `>`, `<`, `>=` og `<=`.

`size=...` er ikke støttet.

## Bredde, høyde og orientering

### `width`

Filtrer etter bildebredde i piksler.

```text
width=1024
width>1024
width>=3000
width<2000
width<=4000
```

Verdien skrives som heltall uten enhet.

### `height`

Filtrer etter bildehøyde i piksler.

```text
height=1024
height>1024
height>=2000
height<2000
height<=4000
```

Verdien skrives som heltall uten enhet.

### `orientation`

Filtrer etter om bildet er stående eller liggende.

```text
orientation:portrait
orientation:landscape
```

| Verdi       | Betydning                    |
| ----------- | ---------------------------- |
| `portrait`  | høyden er større enn bredden |
| `landscape` | bredden er større enn høyden |

Bare filer med registrert bredde og høyde kan matche disse filtrene.

## Kamera og tekstmetadata

### `camera`

Søk i kameramerke og kameramodell.

```text
camera:iPhone
camera:"Canon EOS"
```

Søket er et delstrengsøk mot kameramerke og kameramodell samlet.

## Organisering

### `source`

Filtrer etter importkilde.

```text
source:1
source:"Mobil 2024"
```

Hvis verdien er et tall, tolkes den som kilde-ID.

```text
source:1
```

Hvis verdien ikke er et tall, søkes det i kildenavnet.

```text
source:"Mobil 2024"
```

### `tag`

Filtrer etter tagg.

```text
tag:"Ute av fokus"
tag:ferie
```

Taggnavn sammenlignes normalisert. Bindestrek og understrek behandles som mellomrom i tillegg til nøyaktig normalisert navn.

### `person`

Filtrer etter personnavn i ansiktsdatabasen.

```text
person:Viljar
person:"Ola Nordmann"
```

Søket bruker personnavn i ansiktsdatabasen. Det matcher bilder der personen er knyttet til et ansikt, inkludert forslag.

### `deleted`

Filtrer på slettede filer.

```text
deleted:true
deleted:false
```

`deleted:true` viser filer som er markert som slettet.

`deleted:false` er i praksis standardvisningen, altså aktive filer.

## Manglende informasjon

### `missing:gps`

Viser filer uten GPS-koordinater og uten manuell H3-plassering.

```text
missing:gps
```

### `missing:date`

Viser filer der Bildebank ikke har funnet noen brukbar dato.

```text
missing:date
```

### `missing:metadata`

Viser filer der datoen ikke kommer fra metadata.

```text
missing:metadata
```

## Motion-videoer

Bildebank skjuler normalt motion-videoer når de hører sammen med et stillbilde.

Filtersøk viser motion-videoer når søket eksplisitt handler om video, filendelse eller filnavn, for eksempel:

```text
type:video
extension:mp4
filename:MP
```

## Eksempler

### Alle julaftener

```text
month:12 day:24
```

### Alle 17. mai-bilder

```text
month:5 day:17
```

### Bilder fra adventstiden

```text
month:12 day>=1 day<=25
```

### Sommerbilder

```text
month>=6 month<=8
```

### Bilder fra juli på Kreta

```text
month:7 location:kreta
```

### Store bilder

```text
width>=3000 height>=2000
```

### Små filer

```text
size<300KB
```

### Bilder uten GPS

```text
missing:gps
```

### Bilder med manuell plassering

```text
location:manual
```

### Bilder med detaljert manuell H3-plassering

```text
location:manual h3res>=9
```

### Bilder tatt med iPhone

```text
camera:iPhone
```

### Bilder med en bestemt tagg

```text
tag:"Ute av fokus"
```

### Bilder av en bestemt person i juli

```text
person:Viljar month:7
```

### Slettede filer fra en bestemt kilde

```text
deleted:true source:"Mobil 2024"
```

## Oversikt over kriterier

| Kriterium     | Eksempel                          | Merknad                                      |
| ------------- | --------------------------------- | -------------------------------------------- |
| `after`       | `after:2023-12-01`                | dato etter gitt dato                         |
| `before`      | `before:2024-12-12`               | dato før gitt dato                           |
| `year`        | `year:2024`, `year>=2020`         | år 1–9999                                    |
| `month`       | `month:12`, `month>=6`            | måned 1–12                                   |
| `day`         | `day:24`, `day<=25`               | dag 1–31                                     |
| `date`        | `date:manual`                     | `manual`, `metadata`, `filename`, `mtime`    |
| `location`    | `location:gps`                    | `gps`, `manual` eller stedsslug              |
| `h3res`       | `location:manual h3res>=8`        | H3-oppløsning 0–11, krever `location:manual` |
| `type`        | `type:image`                      | `image`, `video`, `file`                     |
| `extension`   | `extension:jpg`                   | filendelse                                   |
| `filename`    | `filename:IMG`                    | søk i filnavn                                |
| `path`        | `path:2024/01`                    | søk i sti                                    |
| `size`        | `size>2MB`                        | støtter `>`, `<`, `>=`, `<=`                 |
| `width`       | `width>=3000`                     | bredde i piksler                             |
| `height`      | `height>=2000`                    | høyde i piksler                              |
| `orientation` | `orientation:portrait`            | `portrait`, `landscape`                      |
| `camera`      | `camera:iPhone`                   | søk i kameramerke og modell                  |
| `source`      | `source:1`, `source:"Mobil 2024"` | kilde-ID eller kildenavn                     |
| `tag`         | `tag:"Ute av fokus"`              | taggnavn                                     |
| `person`      | `person:Viljar`                   | personnavn i ansiktsdatabasen                |
| `deleted`     | `deleted:true`                    | slettede filer                               |
| `missing`     | `missing:gps`                     | `gps`, `date`, `metadata`                    |

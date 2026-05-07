# import-removable

`import-removable` registrerer og importerer CD-er, USB-brikker, minnekort og
andre flyttbare medier i én kommando.

## Referanse

```powershell
bildebank import-removable --name navn mappe
```

Vanlige valg:

```powershell
bildebank import-removable --name "Familie-CD-2004" --dry-run E:\
bildebank import-removable --name "Familie-CD-2004" E:\
```

`--name` er påkrevd. Bruk et navn du kjenner igjen senere, for eksempel teksten
på CD-en, navnet du har skrevet på USB-brikken, eller et annet stabilt navn.

`mappe` er stasjonen eller mappen der mediet finnes akkurat nå.

Eksempel:

```powershell
bildebank import-removable --name "Sommerbilder-USB" F:\
```

## Når skal du bruke import-removable?

Bruk `import-removable` for kilder som kan forsvinne, bytte stasjonsbokstav
eller bli satt inn igjen senere:

- USB-brikker
- minnekort
- CD-er og DVD-er
- eksterne disker der samme stasjonsbokstav kan brukes av ulike medier

Ikke kjør `add` først for slike medier.

## Hvorfor må man bruke --name?

Windows kan bruke samme stasjonsbokstav for forskjellige medier. I dag kan
`E:\` være en CD, og i morgen kan `E:\` være en USB-brikke.

Derfor bruker Bildebank `--name` som identiteten til mediet. Pathen forteller
bare hvor mediet finnes akkurat når importen kjøres.

Du kan ikke gjenbruke samme `--name` for en ny import. Hvis du importerer flere
deler av samme USB-brikke hver for seg, må hver del få sitt eget navn.

## Tørrtest

Bruk `--dry-run` når du vil se hva som ville skjedd uten å registrere mediet,
kopiere filer eller endre databasen:

```powershell
bildebank import-removable --name "Familie-CD-2004" --dry-run E:\
```

## Spørsmål/svar/forklaringer

`import-removable` er robust, slik at man kan importere først enkeltmapper fra
en usb-brikke, og eventuelt til slutt importere hele brikken. Systemet er
fortsatt trygt, og det lagres ikke duplikate bildefiler. La oss si vi har en
brikke vi kaller "Brikke-A" med 3 mapper som alle inneholder filer:

```powershell
F:\
 +-mappe1
 +-mappe2
 +-mappe3
```

Man må bare bruke unike navn etter `--name`. Og hvilket navn har egentlig ikke
noe å si. Men hvis du har litt system, så skjønner du hva du har gjort i
etterkant. Eksempel

```powershell
C:\fotobank> bildebank import-removable --name "BrikkeA-1" F:\mappe1
C:\fotobank> bildebank import-removable --name "BrikkeA-2" F:\mappe2
C:\fotobank> bildebank import-removable --name "BrikkeA-hele" F:\
```

Etter de tre kommandoene over har du importert alt på usb-brikken, uten at det
har blitt lagret duplikate bildefiler. Og du kan kjøre `unimport` på de to
første for å slippe å ha unødvendige kilder registrert i databasen:

```powershell
C:\fotobank> bildebank unimport --name "BrikkeA-1"
C:\fotobank> bildebank unimport --name "BrikkeA-2"
```

Du kan teste effekten først:

```powershell
C:\fotobank> bildebank unimport --dry-run --name "BrikkeA-1"
```

I eksempelet over er effekten av å ha fjernet `BrikkeA-1` og `BrikkeA-2` at
bildesamlingen i praksis ser ut som om man bare hadde importert hele `F:\` med
`--name "BrikkeA-hele"`.

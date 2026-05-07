# unimport

`unimport` angrer en tidligere importert kilde.

## Referanse

For en vanlig kildemappe:

```powershell
bildebank unimport [valg] mappe
```

For en kilde importert med `import-removable`:

```powershell
bildebank unimport [valg] --name navn
```

Vanlige valg:

```powershell
bildebank unimport --dry-run "$HOME\Pictures\TestBilder"
bildebank unimport --dry-run --name "Familie-CD-2004"
```

## Hva kommandoen gjør

`unimport` fjerner koblingen mellom bildesamlingen og én bestemt kilde.

Hvis en fil bare kom fra denne ene kilden, fjernes filen fra den aktive
bildesamlingen.

Hvis samme fil også finnes i en annen kilde, blir filen liggende. Da fjernes
bare henvisningen til kilden du angrer.

## Dette er en destruktiv kommando

`unimport` kan fjerne filer fra den aktive bildesamlingen. Derfor er kommandoen
forsiktig.

Før den endrer noe, kontrollerer Bildebank at alle registrerte kildefiler
fortsatt finnes i kilden, og at de har nøyaktig samme innhold som da de ble
importert.

Hvis en kildefil mangler eller er endret, stopper kommandoen uten å gjøre
endringer.

## Bruk dry-run først

Start med:

```powershell
bildebank unimport --dry-run "$HOME\Pictures\TestBilder"
```

eller:

```powershell
bildebank unimport --dry-run --name "Familie-CD-2004"
```

Da kontrollerer Bildebank filene og viser hva som ville blitt gjort, men endrer
ikke databasen og sletter ingen filer.

## Bekreftelse

Når du kjører uten `--dry-run`, viser Bildebank en oppsummering før noe endres:

```text
Kilde: C:\Users\Tom\Pictures\TestBilder
Registrerte kildefiler kontrollert: 179
Filer som fjernes fra aktiv samling: 142
Filer som blir liggende fordi de også finnes i andre kilder: 37
Skriv "ja, det vil jeg" for å gjennomføre unimport:
```

For å gjennomføre må du skrive nøyaktig:

```text
ja, det vil jeg
```

Hvis du skriver noe annet, avbryter Bildebank uten å endre noe.

## Vanlige mapper

For en vanlig mappe bruker du path:

```powershell
bildebank unimport "$HOME\Pictures\TestBilder"
```

Etterpå settes kilden tilbake til `pending`. Det betyr at Bildebank husker
kilden, og at den kan importeres igjen senere.

Hvis du ikke vil importere den igjen, kjør `remove-source` etterpå. Bildebank
skriver ut riktig kommando når `unimport` er ferdig.

## Flyttbare medier

For CD-er, USB-brikker, minnekort og andre kilder importert med
`import-removable`, bruker du `--name`:

```powershell
bildebank unimport --name "Familie-CD-2004"
```

Ikke bruk pathen til USB-brikken eller CD-en. Flyttbare medier skal alltid
angis med navnet de fikk ved import.

Når `unimport --name` er ferdig, fjernes kilden også fra kildelisten. Du
trenger ikke kjøre `remove-source` etterpå.

Hvis Bildebank sier at en kildefil mangler, sjekk at riktig USB-disk, CD eller
minnekort er satt inn, og at den har samme stasjon/path som da importen ble
kjørt.


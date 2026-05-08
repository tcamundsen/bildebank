# unimport

`unimport` angrer en tidligere import.

## Referanse

```powershell
bildebank unimport --name navn
```

Vanlige valg:

```powershell
bildebank unimport --dry-run --name "Sommer2023"
bildebank unimport --name "Familie-CD-2004"
```

Bruk samme navn som du brukte da du importerte:

```powershell
bildebank import --name "Sommer2023" "$HOME\Pictures\Sommer2023"
bildebank unimport --name "Sommer2023"
```

## Hva kommandoen gjør

`unimport` fjerner koblingen mellom bildesamlingen og én bestemt import.

Hvis en fil bare kom fra denne ene importen, fjernes filen fra den aktive
bildesamlingen.

Hvis samme fil også finnes i en annen import, blir filen liggende. Da fjernes
bare henvisningen til importen du angrer.

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
bildebank unimport --dry-run --name "Sommer2023"
```

Da kontrollerer Bildebank filene og viser hva som ville blitt gjort, men endrer
ikke databasen og sletter ingen filer.

## Bekreftelse

Når du kjører uten `--dry-run`, viser Bildebank en oppsummering før noe endres:

```text
Kilde: Sommer2023
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

## Hvis kilden mangler

`unimport` må kontrollere originalfilene før noe fjernes. Hvis kilden ligger på
USB, CD eller minnekort, må riktig medium være satt inn når du kjører
kommandoen.

Hvis Bildebank sier at en kildefil mangler, sjekk at riktig USB-disk, CD eller
minnekort er satt inn, og at den har samme stasjon/path som da importen ble
kjørt.

## Etter unimport

Når `unimport --name` er ferdig, fjernes importen også fra kildelisten. Du
trenger ikke kjøre `remove-source` etterpå.

Etterpå kan du lage HTML-visningen på nytt:

```powershell
bildebank make-browser
```


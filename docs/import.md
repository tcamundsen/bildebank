# import

`import` importerer én navngitt kilde direkte.

## Referanse

```powershell
bildebank import --name navn mappe
```

Vanlige valg:

```powershell
bildebank import --name "Sommer2023" "$HOME\Pictures\Sommer2023"
bildebank import --name "Familie-CD-2004" E:\
bildebank import --name "USB-A" --dry-run F:\
bildebank import --name "USB-A" --dry-run --log-file importliste.txt F:\
```

`--name` er påkrevd for vanlig bruk. Navnet må være unikt. Bruk et navn du
kjenner igjen senere, fordi det samme navnet brukes hvis du vil angre importen
med `unimport`.

`mappe` er kilden som skal importeres. Det kan være en vanlig mappe, en
USB-brikke, et minnekort, en CD, en DVD eller en ekstern disk.

## Hva kommandoen gjør

`import` registrerer kilden og importerer støttede bilder og videoer inn i
bildesamlingen.

Filene plasseres etter dato, for eksempel:

```text
2024\01
2024\02
unknown-date
```

Bildebank prøver først å finne dato i metadata. Hvis det ikke går, kan den bruke
dato fra filnavn eller filens endringstidspunkt.

## Tørrtest først

Det er lurt å kjøre med `--dry-run` først:

```powershell
bildebank import --name "Sommer2023" --dry-run "$HOME\Pictures\Sommer2023"
```

Da viser Bildebank hva programmet ville gjort, uten å registrere kilden, uten å
kopiere filer og uten å endre databasen.

Hvis listen ser riktig ut, kjører du samme kommando uten `--dry-run`:

```powershell
bildebank import --name "Sommer2023" "$HOME\Pictures\Sommer2023"
```

## Navn

Navnet etter `--name` er identiteten til importen.

Det er viktig fordi pathen ikke alltid er en trygg identitet. I dag kan `E:\`
være en CD, og i morgen kan `E:\` være en USB-brikke.

Bruk derfor navn som er lette å kjenne igjen:

```powershell
bildebank import --name "Familie-CD-2004" E:\
bildebank import --name "Minnekort-Kamera-2023-07" F:\
bildebank import --name "GamleBilder-PC" "$HOME\Pictures\GamleBilder"
```

Du kan ikke gjenbruke samme navn for en ny import. Hvis du importerer flere
deler av samme USB-brikke hver for seg, må hver del få sitt eget navn.

## Duplikater

Bildebank prøver å unngå å lagre samme bildefil flere ganger. Hvis samme fil
finnes i flere kilder, skal bildet bare ligge ett sted i bildesamlingen, mens
Bildebank husker hvilke kilder bildet kom fra.

## Etter import

Etter import kan du lage HTML-visningen:

```powershell
bildebank make-browser
```

Da får du en `index.html` i bildesamlingen som kan åpnes i nettleseren.


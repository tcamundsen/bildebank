# import

`import` importerer vanlige kilder som tidligere er registrert med `add`.

## Referanse

```powershell
bildebank import [valg]
```

Vanlige valg:

```powershell
bildebank import --dry-run
bildebank import --quiet
bildebank import --dry-run --log-file importliste.txt
```

## Hva kommandoen gjør

`import` går gjennom kildene som er registrert med `add`, og som ikke allerede
er ferdig importert. Støttede bilder og videoer kopieres inn i
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

Det er lurt å kjøre:

```powershell
bildebank import --dry-run
```

Da viser Bildebank hva programmet ville gjort, uten å kopiere filer og uten å
endre databasen.

Hvis listen ser riktig ut, kjører du:

```powershell
bildebank import
```

## Hvis import sier scannet=0

Hvis du kjører `import` en gang til uten å ha lagt til nye kilder, er det
normalt at oppsummeringen viser `scannet=0`.

Det betyr vanligvis bare at det ikke er noe nytt å importere.

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


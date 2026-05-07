# add

`add` registrerer en vanlig kildemappe som senere skal importeres.

## Referanse

```powershell
bildebank add mappe
```

Eksempel:

```powershell
bildebank add "$HOME\Pictures\TestBilder"
```

Etterpå importerer du med:

```powershell
bildebank import
```

## Hva er en kildemappe?

En kildemappe er en mappe der du allerede har bilder eller videoer som skal
kopieres inn i bildesamlingen.

`add` kopierer ikke bilder. Den registrerer bare at Bildebank skal importere
fra denne mappen neste gang du kjører `import`.

## Når skal du bruke add?

Bruk `add` for vanlige mapper på PC-en, en ekstern harddisk som alltid har
samme innhold, eller en mappe du vet ligger fast på samme sted.

Ikke bruk `add` for CD-er, USB-brikker, minnekort og andre flyttbare medier.
Bruk `import-removable` for slike kilder.

## Eksempel

```powershell
cd "$HOME\BildeSamling"
bildebank add "$HOME\Pictures\TestBilder"
bildebank import --dry-run
bildebank import
```

`--dry-run` viser hva som ville blitt importert, uten å kopiere filer og uten å
endre databasen.

## Hvis du la til feil mappe

Hvis du har lagt til feil mappe, men ikke importert den ennå, kan du fjerne den
fra kildelisten:

```powershell
bildebank remove-source "$HOME\Pictures\TestBilder"
```

Hvis mappen allerede er importert, må du først bruke `unimport`.


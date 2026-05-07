# remove-source

`remove-source` fjerner en kilde fra kildelisten.

## Referanse

For en vanlig kildemappe:

```powershell
bildebank remove-source [valg] mappe
```

For en flyttbar kilde:

```powershell
bildebank remove-source [valg] --name navn
```

Vanlige valg:

```powershell
bildebank remove-source --dry-run "$HOME\Pictures\TestBilder"
bildebank remove-source --dry-run --name "Familie-CD-2004"
```

## Hva kommandoen gjør

`remove-source` fjerner en kilde fra listen over kilder Bildebank kjenner til.

Den skal ikke slette bilder fra bildesamlingen.

## Når brukes remove-source?

Bruk `remove-source` når en kilde er registrert, men ikke har aktive importerte
filer i samlingen.

Typiske tilfeller:

- du kjørte `add` på feil mappe, men har ikke importert den ennå
- du har kjørt `unimport` på en vanlig kildemappe
- en vanlig kildemappe er erstattet av en overordnet kilde

## Eksempel: feil mappe ble lagt til

```powershell
bildebank add "$HOME\Pictures\FeilMappe"
bildebank remove-source "$HOME\Pictures\FeilMappe"
```

## Eksempel: etter unimport

```powershell
bildebank unimport "$HOME\Pictures\TestBilder"
bildebank remove-source "$HOME\Pictures\TestBilder"
```

For vanlige mapper setter `unimport` kilden tilbake til `pending`. Hvis du ikke
vil importere den igjen, bruker du `remove-source`.

## Flyttbare medier

For kilder importert med `import-removable`, bruker du vanligvis ikke
`remove-source` etterpå.

Bruk heller:

```powershell
bildebank unimport --name "Familie-CD-2004"
```

Den kommandoen både angrer importen og fjerner kilden fra kildelisten.

`remove-source --name` kan brukes hvis en flyttbar kilde står i kildelisten uten
aktive importerte filer.

## Superseded kilder

En vanlig kildemappe kan bli `superseded` hvis den er dekket av en annen kilde
på et høyere nivå.

Eksempel:

```text
C:\bilder\mappe1
C:\bilder
```

Hvis begge er registrert, kan `C:\bilder\mappe1` være dekket av `C:\bilder`.

Da kan `remove-source` fjerne den nederste kilden fra kildelisten, men bare
hvis Bildebank kan kontrollere at filene også finnes gjennom den overordnede
kilden.

Test først:

```powershell
bildebank remove-source --dry-run "C:\bilder\mappe1"
```


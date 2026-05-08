# remove-source

`remove-source` fjerner en kilde fra kildelisten.

## Referanse

```powershell
bildebank remove-source --name navn
```

Vanlige valg:

```powershell
bildebank remove-source --dry-run --name "Sommer2023"
bildebank remove-source --name "Sommer2023"
```

## Hva kommandoen gjør

`remove-source` fjerner en kilde fra listen over kilder Bildebank kjenner til.

Den skal ikke slette bilder fra bildesamlingen.

## Når brukes remove-source?

Ved vanlig bruk trenger du sjelden `remove-source`.

Hvis en import har aktive filer i bildesamlingen, må du bruke `unimport`:

```powershell
bildebank unimport --name "Sommer2023"
```

`unimport --name` både angrer importen og fjerner kilden fra kildelisten.

`remove-source --name` er bare aktuelt hvis en kilde finnes i kildelisten uten
aktive importerte filer.

## Superseded kilder

En eldre vanlig kildemappe kan være `superseded` hvis den er dekket av en annen
kilde på et høyere nivå.

Eksempel:

```text
C:\bilder\mappe1
C:\bilder
```

Hvis begge er registrert fra en eldre arbeidsflyt, kan `C:\bilder\mappe1` være
dekket av `C:\bilder`.

Da kan `remove-source` fjerne den nederste kilden fra kildelisten, men bare
hvis Bildebank kan kontrollere at filene også finnes gjennom den overordnede
kilden.

Test først:

```powershell
bildebank remove-source --dry-run "C:\bilder\mappe1"
```


# create

`create` gjør en mappe til en bildesamlingsmappe.

## Referanse

```powershell
bildebank create mappe
```

Eksempel:

```powershell
bildebank create "$HOME\BildeSamling"
```

Hvis du allerede står i mappen som skal bli bildesamling, kan du skrive:

```powershell
bildebank create .
```

Punktum betyr "mappen jeg står i nå".

## Hva kommandoen gjør

`create` oppretter Bildebank-databasen i bildesamlingsmappen. Databasen er filen der
Bildebank husker hvilke kilder som er lagt til, hvilke filer som er importert,
og hvor filene ligger i bildesamlingen.

Etter `create` kan du bruke mappen som arbeidsmappe for Bildebank:

```powershell
cd "$HOME\BildeSamling"
```

Deretter kan du importere kilder med `import`:

```powershell
bildebank import --name "TestBilder" "$HOME\Pictures\TestBilder"
```

## Viktig

Bildesamlingen bør ligge i en egen mappe, ikke inni programmappen til
Bildebank.

Ikke bruk en mappe som allerede inneholder masse andre filer du vil rydde
manuelt i. Bildebank kommer til å lage årsmappene, månedsmappene, databasen og
HTML-filer i denne mappen.

## Vanlig arbeidsflyt

```powershell
mkdir "$HOME\BildeSamling"
cd "$HOME\BildeSamling"
bildebank create .
```

Når dette er gjort, kan du kontrollere at Bildebank finner bildesamlingsmappen:

```powershell
bildebank status
```

# remove

`remove` fjerner én importert fil fra den aktive bildesamlingen.

Bruk denne kommandoen når du har funnet et bilde eller en video som ikke skal
være med i bildesamlingen, men der du ikke vil angre en hel import.

## Referanse

```powershell
bildebank remove fil
```

Eksempel:

```powershell
bildebank remove "2024\01\IMG_0001.jpg"
```

`fil` er en fil som allerede finnes i bildesamlingen. Du kan bruke en relativ
sti fra bildesamlingsmappen, slik som i eksempelet over.

Du skal altså peke på filen slik den ligger i bildesamlingen, ikke på
originalfilen i kilden du importerte fra.

## Hva kommandoen gjør

`remove` sletter ikke filen helt. Den flytter filen til `deleted`-mappen inne i
bildesamlingen, og markerer filen som slettet i databasen.

Eksempel:

```text
2024\01\IMG_0001.jpg
```

flyttes til:

```text
deleted\2024\01\IMG_0001.jpg
```

Etterpå vises ikke filen i nettleservisningen.

Originalfilen i kilden blir ikke slettet.

## Når skal du bruke remove?

Bruk `remove` når én bestemt importert fil ikke skal være med i den aktive
bildesamlingen.

Hvis du vil angre en hel import, bruk `unimport` i stedet:

```powershell
bildebank unimport --name "Sommer2023"
```

Kort sagt:

- `remove` brukes for én fil i bildesamlingen.
- `unimport` brukes for en hel importert kilde.

## Se slettede filer

Du kan se filer som er markert som slettet med:

```powershell
bildebank list-removed
```

Listen viser blant annet når filen ble slettet, hvor den lå før, hvor den ligger
nå, og hvilken kildefil den kom fra.

## Hvis kommandoen stopper

Hvis Bildebank sier at filen ikke finnes i importdatabasen, betyr det vanligvis
at du har oppgitt feil filsti, eller at du peker på en fil som ikke er importert
av Bildebank.

Hvis Bildebank sier at filen allerede er markert som slettet, er den allerede
flyttet til `deleted`-mappen.

Hvis Bildebank sier at filen ikke finnes på disk, ligger den i databasen, men
selve filen mangler fra bildesamlingen. Da bør du først undersøke om filen er
flyttet eller slettet manuelt.

## Viktig

Ikke flytt filer manuelt inn og ut av `deleted`-mappen. Bruk Bildebank-
kommandoer, slik at databasen og filene fortsatt stemmer overens.

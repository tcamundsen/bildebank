# remove

<!-- CLI-HELP-START -->
```text
usage: bildebank remove [valg] fil

Flytt en importert fil til deleted/ og marker den som slettet

positional arguments:
  fil         Importert fil som skal fjernes

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`remove` fjerner én importert fil fra den aktive bildesamlingen.

Dette kan du også gjøre fra nettleseren ved å trykke på knappen
**Slett** når du ser på et bilde.

Bruk denne kommandoen når du har funnet et bilde eller en video som ikke skal
være med i bildesamlingen, men der du ikke vil angre en hel import.

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

Hvis bildet er scannet for bildesøk eller ansikter, fjernes disse dataene når
bildet flyttes til `deleted`. Det omfatter også bekreftede ansikter,
personkoblinger og bruk av ansikter i bildet som referanse for forslag.
Personer som er registrert i Bildebank, beholdes.

Hvis du senere bruker [`undelete`](undelete.md), må bildet scannes på nytt for
bildesøk og ansikter. Eventuelle personkoblinger må bekreftes på nytt.

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

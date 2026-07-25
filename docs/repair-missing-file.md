# repair-missing-file

<!-- CLI-HELP-START -->
```text
usage: bildebank repair-missing-file [valg] fil-id gjenopprettet-fil

Kontroller en brukeroppgitt kopi mot databaseført størrelse og SHA-256, og
legg den tilbake uten å overskrive en fil.

positional arguments:
  fil-id             ID-en doctor viser som file #nummer.
  gjenopprettet-fil  En gjenopprettet kopi som ligger utenfor bildesamlingen.

options:
  -h, --help         show this help message and exit
  --apply            Kopier filen inn etter en ny full kontroll.
```
<!-- CLI-HELP-END -->

Kommandoen kan legge tilbake en databaseført bilde- eller videofil som mangler
fra samlingen. Du må selv oppgi en gjenopprettet kopi. Bildebank leter ikke
automatisk etter en fil og velger aldri mellom forskjellige kopier.

Bruk fil-ID-en som `doctor` viser. Kjør først uten `--apply`:

```powershell
bildebank repair-missing-file 979 "D:\Gjenopprettet\oktnov07 063.avi"
```

Dry-run kontrollerer at:

- målfilen faktisk mangler
- databaseposten og kildeinformasjonen er entydige og konsistente
- den oppgitte kopien er en vanlig fil uten lenker og ligger utenfor samlingen
- størrelse og SHA-256 er nøyaktig lik verdiene i databasen
- samlingen ikke har en uavklart filflytting

En redigert, konvertert eller på annen måte endret fil blir avvist, selv om den
ser lik ut.

Les målsti, størrelse og SHA-256 i resultatet. Ta et oppdatert snapshot. Hvis
alt stemmer, kjør samme kommando med `--apply`:

```powershell
bildebank repair-missing-file 979 "D:\Gjenopprettet\oktnov07 063.avi" --apply
```

Bildebank kontrollerer alt på nytt, kopierer via en verifisert midlertidig fil
og nekter å overskrive noe på målstien. Den eksterne kopien beholdes. Kommandoen
endrer ikke hoveddatabasen. En slettet databasepost blir lagt tilbake under
`deleted`, ikke gjort aktiv igjen.

Hvis kommandoen stopper med feil, skal du beholde den gjenopprettede kopien og
undersøke `doctor`-resultatet, databasen og snapshotene før du gjør manuelle
endringer.

# make-people-browser

`make-people-browser` lager HTML-sider for alle personer som er registrert i
ansiktsdatabasen.

## Referanse

```powershell
bildebank make-people-browser
bildebank make-people-browser --month-preview-limit 40
```

Kommandoen lager:

```text
personer.html
person-Kari.html
person-Ola.html
...
```

`personer.html` er en startside med ett kort per person. Kortet viser et
eksempelbilde, antall bilder, antall bekreftede ansikter og antall forslag.

Klikk på en person for å åpne personens egen browser. Der kan du bla i bildene
måned for måned, på samme måte som med `make-person-browser`.

Personsider som lages her er enkle statiske bildebrowsere. Bruk
[`run-server`](run-server.md) for å bekrefte ansikter, se ansiktsbokser og
arbeide videre med forslag.

Hvis du har lagt til nye personer eller koblet flere ansikter til personer, bør
du kjøre `make-people-browser` på nytt.

`face-suggest` kjører vanligvis denne oppdateringen automatisk etter at forslag
er beregnet.

Se også [`make-person-browser`](make-person-browser.md).

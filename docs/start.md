# start

<!-- CLI-HELP-START -->
```text
usage: bildebank start

Åpne Bildebank-vinduet.

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`start` åpner Bildebank-vinduet.

```powershell
bildebank start
```

Dette er den vanlige måten å starte Bildebank på når du vil bruke vinduet i
stedet for å skrive kommandoer i PowerShell.

Fanen **Bildebank** starter bildebrowseren på vanlig måte. Fanen **Nettleser og
deling** kan brukes når du vil velge en annen port, åpne bildebrowseren
skrivebeskyttet eller dele en skrivebeskyttet visning på et privat LAN.
Standardporten er `8765`.

Ved deling på LAN får du alltid en advarsel før en ny server startes. Det er
ingen innlogging, og alle på det samme nettverket kan se bildene. Bruk derfor
bare LAN-deling på et privat nettverk du stoler på.

Den gamle kommandoen `bildebank launcher` virker fortsatt som et alias, men
`bildebank start` er navnet som brukes i dokumentasjonen.

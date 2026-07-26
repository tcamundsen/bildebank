# update

<!-- CLI-HELP-START -->
```text
usage: bildebank update [valg]

Oppdater Bildebank til siste versjon fra GitHub.

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`update` oppdaterer Bildebank-programmet til siste versjon fra GitHub
og laster ned eventuelle nye biblioteker som brukes.

Oppdateringen starter bare når programmappen ikke har lokale endringer i filer
som følges av Git. Andre lokale filer blir liggende og blokkerer ikke
oppdateringen. Hvis en slik fil har samme navn som en ny programfil, stopper
Git oppdateringen uten å overskrive filen. Før nedlasting lagres gjeldende
programversjon. Den nye versjonen installeres og kontrolleres før oppdateringen
regnes som ferdig. Hvis installasjonen feiler, gjenoppretter Bildebank den
gamle programversjonen og kontrollerer at den fortsatt starter. Hvis støtte
for ansiktsgjenkjenning eller OpenCLIP var installert, oppdateres og
kontrolleres også disse. Ved gjenoppretting installeres de samme delene på
nytt fra avhengighetslistene som fulgte med den gamle programversjonen.

Hvis maskinen slås av eller oppdateringen avbrytes, oppdager neste
oppdateringsforsøk recovery-markøren. Bildebank forsøker da først å
gjenopprette den gamle versjonen og ber deg kjøre `bildebank update` én gang
til. Hvis trygg gjenoppretting ikke er mulig, beholdes markøren og Bildebank
stopper uten å overskrive lokale endringer.

Oppdateringen kontrollerer også den lokale FFmpeg-installasjonen. Dermed får
eksisterende brukere støtten som trengs for AVI-avspillingskopier, ikke bare
nye installasjoner. Hvis FFmpeg-nedlastingen feiler, beholdes den fullførte
Bildebank-oppdateringen. Programmet viser en advarsel og prøver igjen ved neste
oppstart.

Eksempel:

```powershell
bildebank update
```

Du kan også starte samme oppdatering fra Bildebank-vinduet med knappen
`Oppdater Bildebank`.

Etter en oppgradering kan det hende programmet ber deg kjøre
`bildebank migrate` for å oppdatere databasen.

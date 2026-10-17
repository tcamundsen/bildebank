# start

<!-- CLI-HELP-START -->
```text
usage: bildebank start

Åpne Bildebank-vinduet.

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

Etter installasjon eller oppdatering på Windows lages en snarvei som heter
**Bildebank** på skrivebordet ditt hvis den mangler. Dobbeltklikk på snarveien
for å åpne Bildebank-vinduet. Eksisterende snarveier beholdes.

Du kan også åpne programmappen `bildebank` i Filutforsker og dobbeltklikke på
`start-bildebank.cmd` for å åpne Bildebank-vinduet. Hvis Windows skjuler
filendelser, vises filen som `start-bildebank`.

Hvis du vil lage snarveien selv, kan du lage en snarvei til filen på
skrivebordet. Lag en snarvei, ikke en kopi av selve filen: startfilen må ligge
i programmappen for å virke.

Hvis oppstarten feiler, blir vinduet med feilmeldingen stående til du trykker
en tast.

Fra PowerShell kan du åpne det samme vinduet med kommandoen `start`:

```powershell
bildebank start
```

I fanen **Verktøy** kan **Grupper bilder …** lage forslag til grupper av
lignende bilder når bildesøk er klargjort. Du velger antall grupper, et
valgfritt filtersøk og en seed. Jobben endrer ingen bilder, tagger eller annen
metadata. Når den er ferdig, åpner du **Gruppering** i bildebrowseren for å se
resultatet.

Fanen **Bildebank** starter bildebrowseren på vanlig måte. Fanen **Nettleser og
deling** kan brukes når du vil velge en annen port, åpne bildebrowseren
skrivebeskyttet, dele en skrivebeskyttet visning på et privat LAN eller starte
et automatisk slideshow på LAN. Standardporten er `8765`.

Velg **Slideshow på LAN** for å vise ett bilde om gangen på for eksempel en TV
eller et nettbrett. **Sekunder per bilde** er `10` som standard og må være et
positivt heltall. Det valgfrie filterfeltet bruker samme uttrykk som Filtersøk,
for eksempel `year=1999` eller `person:Ola tag:Favoritter`. Et tomt filterfelt
viser alle aktive stillbilder som slideshowet kan vise. Adressene som kan åpnes
fra andre enheter, står i fanen.

Ved LAN-deling og slideshow får du alltid en advarsel før en ny server startes.
Det er ingen innlogging, og alle på det samme nettverket kan se bildene. Bruk
derfor bare disse modusene på et privat nettverk du stoler på.

Hvis Bildebank-serveren allerede kjører med de samme valgene, åpnes den
eksisterende adressen. Hvis du endrer modus, port, filter eller sekunder per
bilde, spør Bildebank om serveren skal startes på nytt. Velger du **Avbryt**,
fortsetter den eksisterende serveren uendret. Ved omstart stoppes den gamle
serveren kontrollert før den nye startes.

Den gamle kommandoen `bildebank launcher` virker fortsatt som et alias, men
`bildebank start` er navnet som brukes i dokumentasjonen.

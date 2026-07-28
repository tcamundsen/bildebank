# Permanent sletting

Når du bruker **Slett** i Bildebank, flyttes bildet først til papirkurven i
bildesamlingens `deleted`-mappe. Filen er ikke slettet permanent og kan flyttes
tilbake med **Undelete**.

På siden **Innstillinger → Slettede bilder** kan du senere slette én fil
permanent eller tømme hele papirkurven.

## Før du sletter permanent

Permanent sletting fjerner filen fra den aktive bildesamlingen. Den kan ikke
angres med **Undelete** etterpå.

Eldre snapshots, andre sikkerhetskopier og mappen du opprinnelig importerte
fra, kan fortsatt inneholde egne kopier. Bildebank endrer eller sletter ikke
disse. For eksempel kan et bilde fortsatt finnes i
`F:\Gamle bilder\IMG_0001.jpg` selv om kopien i bildesamlingen er slettet
permanent.

Prøv funksjonen i en disponibel testsamling før du bruker den i den vanlige
bildesamlingen.

## Slette én fil permanent

1. Start Bildebank-serveren i vanlig, skrivbar modus.
2. Åpne **Innstillinger → Slettede bilder**.
3. Finn filen og trykk **Slett permanent**.
4. Kontroller filnavn og størrelse i bekreftelsen.
5. Les advarselen og trykk **Slett permanent** én gang til.

Bildebank kontrollerer at det fortsatt er nøyaktig den samme filen som ble
forhåndsvist. Hvis filen eller registreringen er endret, slettes den ikke. Last
inn siden på nytt og undersøk hva som har skjedd.

## Tømme papirkurven

Knappen **Tøm papirkurven** viser hvor mange filer som skal slettes og hvor mye
plass de bruker. Bekreftelsen gjelder bare filene som var med i
forhåndsvisningen. En ny fil som flyttes til `deleted` mens dialogen er åpen,
blir ikke tatt med.

Hvis noen filer blir slettet og andre ikke kan slettes, viser Bildebank:

> Enkelte filer kunne ikke slettes.

Trykk **Lukk**. Siden lastes inn på nytt og viser filene som fortsatt trenger
oppfølging.

Ukjente filer som noen har lagt manuelt under `deleted`, blir aldri tatt med
automatisk. Ikke flytt filer manuelt inn eller ut av denne mappen.

## Hvis slettingen ikke blir ferdig

Bildebank lagrer en sikker intern journal før den fysiske filen fjernes. En
ufullført sletting vises fortsatt på siden:

- **Permanent sletting venter etter en feil** betyr at originalen fortsatt
  finnes uendret. Du kan velge **Prøv igjen** eller **Avbryt permanent
  sletting**.
- **Venter på å fullføre permanent sletting** betyr at originalen allerede er
  borte, men at Bildebank må fullføre registreringen. Bare **Prøv igjen** er
  tilgjengelig.
- **Permanent sletting krever kontroll** betyr at noe uventet finnes på
  filstien. Bildebank sletter det ikke automatisk.

**Avbryt permanent sletting** fjerner bare den ventende slettehandlingen.
Filen blir liggende i papirkurven og kan deretter flyttes tilbake med
**Undelete**.

## Slettingsmarkører

Etter en vellykket permanent sletting oppretter Bildebank en liten
slettingsmarkør. Den inneholder ikke bildet. Markøren gjør at nøyaktig samme
filinnhold ikke blir importert til samlingen igjen fra en annen mappe,
USB-disk eller sikkerhetskopi.

Slettingsmarkørene vises nederst på siden **Slettede bilder** med ID,
opprinnelig filnavn, tidligere plassering, størrelse og tidspunkt.

Hvis filinnholdet skal kunne importeres igjen:

1. Finn riktig markør.
2. Trykk **Fjern slettingsmarkør**.
3. Kontroller opplysningene og bekreft.
4. Kjør en vanlig import fra en mappe som fortsatt inneholder filen.

Å fjerne markøren gjenoppretter ingen fil og starter ingen import. Det åpner
bare for at samme filinnhold kan importeres ved en senere import.

## Read-only og deling på nettverket

Permanent sletting og administrasjon av slettingsmarkører er ikke tilgjengelig
når serveren kjører med `--read-only` eller `--lan-share`.

En eksternt tilgjengelig, skrivbar server har ingen innlogging. Alle som kan nå
den, kan også bruke permanente slettehandlinger. Bruk derfor vanlig lokal
server, eller del bare med `--lan-share`, med mindre du uttrykkelig stoler på
alle klientene.

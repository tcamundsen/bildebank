# Se og finne bilder i Bildebank

Denne veiledningen handler om Bildebank i nettleseren. Den forklarer hvordan du
kan bla i bildesamlingen, finne bestemte bilder og gjøre enkle endringer.

Noen knapper og menyvalg vises bare når den tilhørende funksjonen er
tilgjengelig. Hvis Bildebank er åpnet uten skrivetilgang, kan du se og bla i
bildene, men knapper som endrer noe, er skjult.

## Finn fram i bildesamlingen

Forsiden viser bildesamlingen ordnet etter år. Hvert år har et eksempelbilde og
viser hvor mange måneder og bilder året inneholder.

1. Klikk på et år for å se månedene i året.
2. Klikk på en måned for å se alle bildene i måneden.
3. Klikk på et miniatyrbilde for å åpne bildesiden.

Øverst på siden vises en sti, for eksempel:

```text
År / 2024 / Januar / 14 / IMG_1234.JPG
```

Klikk på dagen for å gå til det første bildet fra samme dag. Klikk på måneden,
året eller **År** for å gå tilbake til den tilsvarende oversikten. Klikk på
filnavnet for å åpne **Bildeinfo**. Der finner du blant annet dato, filtype,
størrelse, kameraopplysninger og hvor filen er lagret i bildesamlingen.
Filtypen og kameraet er lenker til tilsvarende filtersøk. Hver importkilde er
en lenke til alle aktive bilder fra den kilden.

**Meny** øverst til høyre gir tilgang til søk, tilfeldige bilder, personer,
steder, tagger, innstillinger og andre oversikter. Når du er inne i et
søkeresultat eller en annen avgrenset visning, bruker du **Alle bilder** for å
gå tilbake til hele bildesamlingen.

På **Dashboard** er samlingstallene lenker til de tilsvarende bildene eller
oversiktene. Du kan for eksempel åpne alle bilder, alle videoer, filer uten
dato, slettede bilder eller importerte mapper direkte fra tallene.

## Bildesiden

Bildesiden viser ett bilde, én video eller én annen fil om gangen.

- Klikk på et vanlig bilde for å åpne en større visning i en ny fane. Lukk
  fanen for å gå tilbake til bildesiden.
- Videoer har vanlige knapper for avspilling, lyd og spoling.
- Filtyper nettleseren ikke kan vise som bilder, vises som en lenke til filen.
- En RAW-, NEF- eller PSD-fil som hører sammen med et JPG-bilde, kan vises som
  en egen knapp på JPG-bildets side.
- Hold musepekeren over en knapp hvis du er usikker. De fleste knappene har en
  kort forklaring.

Sidefeltet ved bildet kan vise kommentarer, tagger, sted og personer i bildet.
På en smal skjerm flyttes feltene over bildet i stedet for å ligge på siden.

## Bla med knapper, tastatur eller berøring

Knappene over bildet er samlet etter hva de blar mellom:

- **År** går til forrige eller neste år.
- **Måned** går til forrige eller neste måned.
- **Bilde** går til forrige eller neste fil i den visningen du bruker.

Hvis du for eksempel har åpnet bilder med en bestemt tagg eller et filtersøk,
blar **Bilde** bare mellom bildene som hører til dette utvalget.

Du kan også bruke tastaturet:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde eller fil |
| Pil høyre | Neste bilde eller fil |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |
| T | Vis et tilfeldig bilde |

Hurtigtastene gjør ingenting mens du skriver i et felt eller har et
dialogvindu åpent. På en berøringsskjerm kan du sveipe mot venstre for neste
bilde og mot høyre for forrige bilde.

## Se tilfeldige bilder

Velg **Meny → Tilfeldig bilde**, eller trykk `T`. Dette er en enkel måte å gå
på oppdagelsesferd i samlingen på. Trykk `T` flere ganger for å fortsette med
nye tilfeldige bilder.

Hvis du står i et filtersøk eller blar i bilder for en tagg, person, kilde,
gruppe eller et definert sted, heter menyvalget **Tilfeldig i utvalget**.
Da velges bildet bare blant bildene i denne visningen, og du fortsetter å bla
innenfor det samme utvalget.

Bildebank foretrekker bilder som ikke er registrert som sett. Når alle er
sett, velges det blant bildene som det er lengst siden du så. Et bilde regnes
først som sett når den vanlige bildesiden har vært synlig en liten stund; det
er ikke nok at bildet har vært vist som et miniatyrbilde.

## Finn bestemte bilder

Bildebank har to forskjellige typer søk:

- **Filtersøk** finner bilder ut fra opplysninger som dato, filnavn, filtype,
  kommentar, tagg, person eller sted.
- **Bildesøk** prøver å finne bilder som visuelt passer til en beskrivelse.

Bruk filtersøk når du vet noe konkret om bildet. Bruk bildesøk når du husker
hva bildet viser, men ikke når eller hvor det ble tatt.

### Filtersøk

Åpne **Meny → Filtersøk**. Skriv ett eller flere kriterier og trykk **Søk**.

| Det du vil finne | Søk |
| --- | --- |
| Bilder fra 2024 | `year:2024` |
| Bilder fra julaften, uansett år | `month:12 day:24` |
| Videoer | `type:video` |
| Et filnavn | `filename:IMG_1234` |
| Bilder uten GPS | `missing:gps` |
| Bilder med taggen Familie | `tag:Familie` |
| Bilder av Kari | `person:Kari` |
| Kommentarer som inneholder «hytta» | `comment:hytta` |

Flere kriterier betyr **og**. Søket

```text
person:Kari month:7 type:image
```

viser derfor bilder av Kari fra juli. Bruk anførselstegn rundt navn med
mellomrom:

```text
tag:"Ute av fokus"
```

På resultatsiden kan du bla mellom treffene med de vanlige bilde-, måneds- og
årsknappene. En [fullstendig oversikt over filtersøk](/help/web/filtersok.md)
finnes på den egne hjelpesiden.

### Bildesøk

Hvis **Bildesøk** finnes i menyen, kan du søke etter innholdet i bildene. Det
fungerer best med en kort beskrivelse på engelsk, for eksempel:

```text
a child holding a cat
a photo of a beach
a red car in winter
```

Feltet ved siden av søketeksten bestemmer hvor mange treff som skal vises.
Første søk kan bruke litt tid mens søkefunksjonen gjøres klar.

Bildesøk er ikke et vanlig nøkkelordsøk og gir ikke en sikker fasit. Det viser
bildene Bildebank mener ligner mest på beskrivelsen, også når ingen bilder
passer særlig godt. Prøv en annen eller mer presis beskrivelse hvis treffene
er dårlige.

På en vanlig bildeside kan knappen **🔍≈** vises. Den finner andre bilder som
ligner på bildet du ser på. Knappen vises bare når denne typen søk er
tilgjengelig, og ikke på videosider.

## Finn bilder via tagger, personer og steder

### Tagger

Velg **Meny → Tagger** for å se alle tagger og hvor mange bilder som har hver
tagg. Klikk på antallet for å åpne disse bildene.

Du kan opprette en ny tagg på samme side. På bildesiden vises tagger som bildet
allerede har, under **På bildet**. Klikk på en av dem for å fjerne den. Velg
**Legg til tagg**, og skriv eventuelt i **Finn tagg**, for å finne og legge til
en annen tagg. **Ute av fokus** har en egen knapp. Hvis sidefeltet inneholder
mange valg, kan du rulle i det uten at bildet flyttes.

Hvis du endrer navn på en tagg, endres navnet for alle bilder som bruker den.
Hvis du sletter en tagg, fjernes bare selve taggen og koblingen til bildene;
bildefilene slettes ikke.

### Personer

Hvis **Personer** finnes i menyen, viser siden registrerte personer og hvor
mange bilder som er knyttet til hver person. Klikk på en person eller et
bildetall for å åpne personens bilder.

Navn under **Personer i bildet** på en bildeside er også lenker til alle
bildene av den personen. Hvis knappen **[+]** vises, kan du bruke den til å
legge til en registrert person manuelt.

### Steder

Velg **Meny → Steder** for å se bilder som har registrert GPS-posisjon eller
tilhører et navngitt sted. Klikk på et sted for å åpne bildene derfra.

Hvis knappen **🌐** vises på en bildeside, kan et bilde uten GPS knyttes til et
forhåndsdefinert sted. Et allerede registrert sted vises i sidefeltet.

## Gjør enkle endringer

Endringsknappene nedenfor vises bare når Bildebank er åpnet med skrivetilgang.

### Roter visningen av et bilde

Bruk **↺** for å rotere mot venstre og **↻** for å rotere mot høyre. Bildebank
lagrer hvordan bildet skal vises, men endrer ikke selve bildefilen. Den valgte
rotasjonen brukes neste gang bildet vises.

### Skriv en kommentar

Klikk **Kommentar** i sidefeltet, skriv teksten og velg **Lagre**. Kommentaren
vises sammen med bildet på bildesiden. Den kan også brukes i filtersøk.

Åpne **Kommentar** igjen for å endre teksten eller velge **Fjern kommentar**.
Dette påvirker ikke bildefilen.

### Korriger datoen

Klikk **📅** for å angi en eksakt dato, en usikker dato eller et datointervall.
Du kan også skrive et kort notat om dateringen.

Den manuelle datoen brukes når Bildebank sorterer og viser bildet etter dato.
Bildefilen flyttes eller endres ikke. Åpne samme dialog igjen hvis datoen skal
endres eller fjernes.

### Flytt et bilde til slettede bilder

Knappen **Slett** flytter bildet ut av den aktive bildesamlingen og til
Bildebanks område for slettede bilder. Du må bekrefte handlingen. Bildet blir
ikke slettet permanent.

Hvis du angrer, åpner du **Meny → Innstillinger → Slettede bilder** og klikker
**Undelete** ved bildet. Valgene for permanent sletting er egne handlinger og
bør bare brukes når du faktisk ønsker at filen ikke lenger skal kunne
gjenopprettes fra Bildebank.

## Innstillinger som gjelder bildebrowseren

Under **Meny → Innstillinger** finnes noen valg som påvirker vanlig bruk:

- **Skjul bilder tagget “Ute av fokus”** fjerner disse bildene fra vanlig
  blaing og søkeresultater. Bildene slettes ikke og kan fortsatt åpnes via
  taggen **Ute av fokus** på siden **Tagger**.
- **Regn stillbilder som sett etter** bestemmer hvor lenge en bildeside må være
  synlig før den påvirker valget av tilfeldige bilder. Valget gjelder bare i
  den aktuelle nettleseren.
- **Hurtigtaster 1–5** kan brukes til ofte gjentatte handlinger, for eksempel å
  sette en tagg, rotere et bilde eller angi dato, sted eller person. Når de er
  aktivert, vises **Hurtigtaster aktivert** ved bildet. Klikk på overskriften
  for å skjule eller vise forklaringene. På en smal skjerm er forklaringene
  skjult til du åpner dem.

`T` for tilfeldig bilde virker uavhengig av de konfigurerbare hurtigtastene
`1` til `5`.

## Hvis en knapp eller et menyvalg mangler

Det betyr vanligvis én av disse tingene:

- Bildebank er åpnet uten skrivetilgang, så endringer er ikke tillatt.
- Funksjonen er ikke tilgjengelig for denne bildesamlingen.
- Handlingen passer ikke til filen du ser på, for eksempel rotering av en
  video eller bildelikhetssøk på en fil som ikke er et bilde.

Du kan fortsatt bruke de vanlige år-, måneds- og bildevisningene selv om noen
av tilleggsfunksjonene mangler.

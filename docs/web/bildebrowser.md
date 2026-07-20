Litt skriblerier om programmet [kan du lese her](om-bildebank.md).
[Hvordan importere de første bildene](kom-i-gang.md)

Oversikt over alle kommandoer [finner du her](../reference.md).


# Bildebrowser

Her beskrives funksjonene til bildebrowseren i nettleseren, dvs nettsiden
du får når du klikker **Start Bildebank i nettleser** fra Bildebank-vinduet.

Første side i bildebrowseren viser en side der du ser første bilde
fra hvert år du har bilder fra. Når du klikker et bilde sendes du til
månedsvisningen, som viser et bilde for hver måned du har bilder fra. Når du
klikker på et bilde i månedsvisningen, åpnes bildesiden. Dette er visningen du
kommer til å bruke mest.

Du kan bla i filene med pilknappene på nest øverste linje på siden.
Men aller enklest er det å bruke tastaturet:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde eller fil |
| Pil høyre | Neste bilde eller fil |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |


Hold musepekeren over knapper og lenker, så får du i de fleste tilfellene opp
en liten hjelpetekst.

Knappene med avkrysningsboks til venstre for bildet brukes for å sette
tagger på bildet som vises. Du kan definere egne tagger ved å klikke
[Tagger](/tags)-lenken øverst og til høyre på siden.

Under **Personer i bildet** i venstrepanelet kan du trykke **[+]** for å få
opp **Velg person**, **Legg til** og **Ferdig**. Dette brukes til å markere at
en person er i bildet manuelt, noe som er nyttig hvis ansiktet til personen
ikke gjenkjennes av insightface. Visningen av denne kan slås av og på i
[innstillinger](/settings).

Du får mer informasjon om bildet ved å klikke på filnavnet øverst på skjermen.

## Kommentarer

Knappen **Kommentar** i venstrepanelet åpner et felt der du kan skrive eller
endre én kommentar til bildet, videoen eller filen. Linjeskift bevares. Bruk
**Fjern kommentar** hvis kommentaren skal tas bort; Bildebank ber om
bekreftelse først.

Kommentaren vises nederst på mediet i full bildevisning. Den vises også i
personbrowser, kilde-, tagg-, sted- og filtersøkvisninger, men ikke på
oversiktsbilder eller miniatyrer i søkeresultater. I read-only/LAN-modus kan
kommentaren leses, men knappen for å redigere den er skjult.

`.NEF`, `.RAW` og `.PSD` vises som lenker til filer, ikke som vanlige bilder.
Når en `.NEF`- eller `.PSD`-fil hører trygt sammen med en `.JPG`-fil fra samme
mappe og samme import, skjules filen fra vanlig bildevisning. Da vises den i
stedet som en lenke på JPG-bildets side.

## Hurtigtaster

Tastene `1`, `2`, `3`, `4` og `5` kan settes opp i **Innstillinger**. Hver
tast kan ha sin egen handling:

- sette bildet til en valgt H3-celle når bildet mangler GPS
- sette en forhåndsvalgt manuell dato
- legge til en valgt person under **Personer i bildet**

Taster som ikke er satt opp gjør ingenting.

I **Innstillinger** kan du slå hurtigtastfunksjonen av og på. Når den er på,
viser bildebrowseren hurtigtastene i venstrefeltet ved bildet, med korte linjer
som for eksempel `1: H3 til Brevik`, `3: Legg til Viljar` eller
`5: Sett dato til 30.12.48 ±1w`. Når funksjonen er av, virker ikke tastene.

## Manuell dato

Knappen 📅 lar deg sette manuell dato for bildet. Du kan velge eksakt dato,
usikker dato eller et datointervall, og legge inn et notat. Bildebank lagrer
dette i databasen og bruker datoen i bildebrowserens
sortering og månedsvisning. Bildefilen flyttes ikke og endres ikke.

## Rotere bilder

På bildesider kan du bruke knappene **↺** og **↻** for å rotere
visningen av bildet. Bildebank lagrer bare rotasjonen i databasen. Selve
bildefilen i samlingen endres ikke.

## Personbrowser

Hvis det står navnet til en person øverst til venstre i vinduet er det fordi du
kjører personbrowseren, som viser bilder du har bekreftet og forslag fra
`face-suggest` for denne personen. Dette moduset har du valgt for
eksempel ved å klikke på knappen med navnet til personen.

- På siden **Personer** åpner **Referansebilder** en oversikt over bildene som
  har bekreftede ansikter brukt som referanser av `face-suggest`. Tallet
  **Foreslåtte bilder** under hvert bilde viser hvor mange forslag som peker
  tilbake på de bekreftede ansiktene i akkurat dette bildet.
- **Alle bilder** viser nettopp det. Hvis du viser et bilde via et filtersøk
  eller personbrowseren, lar **Alle bilder**-lenken deg se bildet der det
  står i hele samlingen.
- Knappen 👤 slår av og på en ramme rundt ansiktet til personen du viser
  personbrowseren av.
- **[✓] Ta med forslag** i verktøylinjen betyr at browseren også viser
  forslagene fra `face-suggest`.
- **[ ] Ta med forslag** i verktøylinjen betyr at browseren bare viser bilder
  du har bekreftet. Dette bør ideelt sett bare være 1-5 gode bilder av
  personen.

Litt skriblerier om programmet [kan du lese her](om-bildebank.md).
Oversikt over alle kommandoer [finner du her](../reference.md).
# Bildebrowseren

Litt om bildebrowseren fra run-server. Jeg vet at noen ting er litt rotete
og preget av work-in-progress. Det er fordi det er det. Men det finpusses
stadig. 

For å gjøre lasting av månedsvisningen raskere kan du laga thumbsnails
av bildene med å kjøre [`make-thumbnails`](../make-thumbnails.md).

For at geo-lokalisering skal fungere må [`geo-scan'](../geo-scan.md)
være kjørt.

Det har kommet en boks med label "Person i bildet", med dropdown meny
og knappene **Legg til** og **Slett**. Denne brukes til å markere at
en person er i bildet manuelt, noe som er nyttig hvis ansiktet til personen
ikke gjenkjennes av insightface.

Og info om bildet som vises får du ved å klikke på filnavnet til bildet,
som vises øverst på skjermen.

## Hurtigtaster

Når HTML-filen er åpen i nettleseren, kan du bla med tastaturet:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige fil |
| Pil høyre | Neste fil |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |

Hvis du har slått på manuel H3-lokalisering, så er tasten `g` hurtigtast for knappen "Sett sted XXX"

RAW/NEF og PSD kan ligge i samlingen, men nettleseren viser dem som fil-lenker
i stedet for vanlig bildevisning.

## Manuell dato

På en bildeside kan du bruke `Sett dato` eller `Endre dato` for å korrigere
datoen Bildebank bruker i browseren. Du kan sette eksakt dato, usikker dato
eller et intervall, og du kan legge inn et notat om hvorfor datoen er satt.

Dette endrer bare Bildebank-databasen. Originalfilen flyttes ikke og endres
ikke. Hvis du fjerner manuell dato, bruker Bildebank igjen datoen som ble funnet
ved import eller metadataoppfrisking.

## Venstre øverste hjørne

Her står det **Bildebrowser** når du starter. Det betyr at du ser alle bildene i samlingen.

Den øverste linjen vises også når du går videre til andre sider i lokalserveren,
for eksempel Personer, Filtersøk, Kilder, Steder, Bildesøk, Hjelp og
Innstillinger. Bruk denne linjen som hovednavigasjon tilbake til de viktigste
delene av bildebanken.

## Filtersøk

Filtersøk lar deg skrive enkle tekstkriterier og bla i treffene som en vanlig
bildebrowser. Dette er nyttig når du skal håndtere spesialtilfeller. Åpne
`Filtersøk` i toppmenyen for søkefelt og oversikt over kriteriene.

Knappene med navn til venstre for **Bildebrowser** lenker til personbrowseren
for denne personen. Hvis knappen har en markering som dette, ✅, så har du
bekreftet personen i dette bildet.

### Personbrowser
Hvis det står navnet til bare en person er det fordi du kjører person-browseren
som viser bekreftede (av deg) og forslag (fra face-suggest) på bilder av denne
personen. Dette moduset har du valgt for eksempel ved å klikke på knappen med
navnet til personen på øverste linje av skjermen.

- **Alle bilder** viser det nettop det.
- **Uten ansiktsmarkeringer** eller **Med ansiktsmarkeringer** tegner en ramme
  rundt ansiktet til personen du viser personbrowseren av.
- **Med forslag** viser også forslagene fra `face-suggest`
- **Bare bekreftede** viser bare bilder du har bekreftet. Dette bør ideelt
  sett bare være 1-5 gode bilder av personen.

I vanlig bildevisning kan feltet **Person i bildet** brukes når personen er i
bildet, men ansiktet ikke kan scannes eller bekreftes godt nok. Feltet bruker
bare personer som allerede finnes i ansiktsdatabasen. Dette påvirker ikke
`face-suggest`, og bildet vises ikke under **Bare bekreftede**.

## Personer

På siden **Personer** kan du trykke `endre navn` bak et personnavn for å åpne
et lite vindu der navnet kan endres. Bekreftede ansiktskoblinger og forslag
beholdes.

Du kan også trykke `slett person` bak et personnavn. Dette sletter personen,
bekreftede ansiktskoblinger, manuelle person-i-bilde-koblinger og forslag for
personen fra ansiktsdatabasen. Det sletter ingen bilder og ingen scannede
ansikter.

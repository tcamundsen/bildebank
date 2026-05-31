Litt skriblerier om programmet [kan du lese her](om-bildebank.md).
Oversikt over alle kommandoer [finner du her](../reference.md).
# Bildebrowseren

Litt om bildebrowseren fra run-server. Jeg vet at noen ting er litt rotete
og preget av work-in-progress. Det er fordi det er det. Men det finpusses
stadig. 

## Hurtigtaster

Når HTML-filen er åpen i nettleseren, kan du bla med tastaturet:

| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde eller video |
| Pil høyre | Neste bilde eller video |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |

Hvis du har slått på manuel H3-lokalisering, så er tasten `g` hurtigtast for knappen "Sett sted XXX"

## Venstre øverste hjørne

Her står det **Bildebrowser** når du starter. Det betyr at du ser alle bildene i samlingen.

Den øverste linjen vises også når du går videre til andre sider i lokalserveren,
for eksempel Personer, Kilder, Steder, Bildesøk, Hjelp og Innstillinger. Bruk
denne linjen som hovednavigasjon tilbake til de viktigste delene av
bildebanken.

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

## Personer

På siden **Personer** kan du trykke `endre navn` bak et personnavn for å åpne
et lite vindu der navnet kan endres. Bekreftede ansiktskoblinger og forslag
beholdes.

Du kan også trykke `slett person` bak et personnavn. Dette sletter personen,
bekreftede ansiktskoblinger og forslag for personen fra ansiktsdatabasen. Det
sletter ingen bilder og ingen scannede ansikter.

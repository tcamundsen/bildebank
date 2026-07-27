# Database v20: opprydding av avledede mediefiler

Status: `schema_version=20` utfører én kontrollert opprydding av foreldreløse
thumbnails og videoavspillingskopier. Etter migreringen holder `unimport`
cacheområdet ryddig når den siste kildereferansen til et bilde fjernes.

## Bakgrunn

Før v20 fjernet `unimport` originalfilen, `files`-raden og bildeavhengige
OpenCLIP- og InsightFace-rader, men kunne la følgende regenererbare filer
ligge igjen:

- thumbnails under `thumbs/v2`
- videoavspillingskopier under `video-previews/v1`
- eldre thumbnails under `thumbs/<år>/<måned>` og `thumbs/udatert`
- avbrutte `.tmp`- og `.partial`-filer i de samme programstyrte strukturene

V20 endrer ikke formatet til disse filene. Databaseversjonen fungerer som en
varig markør på at hele samlingen er kontrollert én gang.

## Eierskap

Migreringen bygger et sett av forventede avledede stier fra alle gjeldende
`files`-rader:

- gjeldende thumbnailsti avledes fra aktiv `target_path`
- for en fil under `deleted/` avledes thumbnailstien fra
  `deleted_original_target_path`
- videoavspillingskopien avledes fra filens SHA-256

Dermed beholdes avledede filer for både aktive bilder og bilder i
papirkurven. Det utgåtte thumbnail-formatet regnes alltid som regenererbart og
kan ryddes.

## Sikker skanning

Skanningen er begrenset til de kjente layoutene under `thumbs` og
`video-previews/v1`. Den:

- går bare inn i forventede år-, måned-, profil- og hashmapper
- gjenkjenner bare de eksakte filnavnformatene Bildebank lager
- følger aldri symlinker, junctions eller Windows reparse points
- ignorerer ukjente filer og mapper
- endrer ikke originalbilder, generert HTML, eksporter eller snapshots

Gjenkjennelige filer som er utrygge eller endres under stabil hashing, blir
stående og rapporteres.

## Pending-delete

En foreldreløs, vanlig fil hashes stabilt og legges i
`pending_file_deletes` med forventet størrelse og SHA-256 i samme
databasetransaksjon som setter `schema_version=20`. Først etter commit forsøker
CLI-en fysisk sletting.

`pending_file_deletes` godtar fra v20 også de strengt gjenkjennelige
cachebanene. Ved hvert forsøk kontrolleres:

- at banen fortsatt har en kjent cachelayout
- at ingen nåværende `files`-rad eier den avledede banen
- at alle stikomponenter og selve filen er vanlige og uten lenker/reparse
  points
- at størrelse og SHA-256 fortsatt stemmer
- at filidentiteten ikke endres mellom kontroll og sletting

En låst eller endret fil beholdes i køen med feilinformasjon. Hvis en senere
import har tatt cachebanen i bruk igjen, nekter oppryddingen å slette filen.

## Ny `unimport`-atferd

Når en `unimport` fjerner den siste `file_sources`-koblingen og dermed
`files`-raden, kølegges eksisterende gjeldende thumbnail og eventuell
videoavspillingskopi før databaseendringen committes. Cachefilene identifiseres
med størrelse og SHA-256 på samme måte som originalen.

Hvis en annen kilde fortsatt peker på bildet, fjernes verken `files`-raden
eller de avledede filene. `--dry-run` og bekreftelsesvisningen viser de
avledede filene som vil bli lagt i køen.

## Tester

Regresjonstestene dekker:

- sletting av thumbnail og videoavspillingskopi ved siste `unimport`
- bevaring når en annen kilde fortsatt peker på bildet
- v19 til v20 med foreldreløse, eide, midlertidige og eldre cachefiler
- bevaring av ukjente filer
- køføring av en låst avledet fil
- avvisning av sletting når en ny `files`-rad har overtatt cachebanen
- bevaring av innhold bak en symlinket cachemappe

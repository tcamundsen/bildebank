# Liste over alle kommandoer

Bildebank begynte som et program der alt måtte gjøres fra PowerShell, og vi kun
bladde i bildene i en statisk HTML-fil. I dag kan du gjøre alle de vanlige
kommandoene fra Bildebank-vinduet eller fra nettleservinduet du åpner derfra,
slik at du skjelden eller aldri trenger å kjøre dem selv i PowerShell.

Kommandoer merket **cli** ("command line interface") kan bare kjøres fra
PowerShell.

Kommandoer merket **vindu** kjøres fra Bildebank-vinduet som åpnes med
`bildebank start`, i tillegg til fra PowerShell.

Merket **web** betyr at du kan få gjort det samme i nettleseren som det
kommandoen gjør i PowerShell.

Merket **devel** betyr at kommandoen ikke er ment for sluttbrukere. De som ikke
er merket har jeg ikke sett over ennå.

## Kom i gang

- [`start`](start.md) **`vindu`**
- [`create`](create.md) **`vindu`**
- [`import`](import.md) **`vindu`**
- [`config`](config.md) **`web`**

## Se og kontrollere samlingen

- [`run-server`](run-server.md) **`vindu`**
- [`make-thumbnails`](make-thumbnails.md) **`vindu`**
- [`make-video-previews`](make-video-previews.md) **`vindu`**
- [`status`](status.md) **`cli`**
- [`list-sources`](list-sources.md) **`web`**
- [`show-source`](show-source.md) **`web`**
- [`check-source`](check-source.md) **`vindu`**
- [`rescan-source`](rescan-source.md) **`vindu`**
- [`date-set`](date-set.md) **`web`**
- [`date-clear`](date-clear.md) **`web`**

## Rydde

- [`remove`](remove.md) **`web`**
- [`undelete`](undelete.md) **`web`**
- [`unimport`](unimport.md) **`vindu`**
- [`cleanup-pending-deletes`](cleanup-pending-deletes.md) **`vindu`**
- [`list-removed`](list-removed.md) **`web`**

## Programmet

- [`where-is`](where-is.md) **`cli`**
- [`doctor`](doctor.md) **`vindu`**
- [`snapshot`](snapshot.md) **`vindu / cli`**
- [`migrate`](migrate.md) **`vindu`**
- [`vacuum`](vacuum.md) **`vindu`**
- [`update`](update.md) **`vindu`**
- [`ffmpeg-install`](ffmpeg-install.md) **`cli`**

## Generering av statiske HTML-filer:

- [`make-browser`](make-browser.md) **`vindu`**
- [`make-people-browser`](make-people-browser.md) **`vindu`**
- [`make-person-browser`](make-person-browser.md) **`vindu`**

## Ansiktsgjenkjenning

- [`insightface`](web/insightface.md) - innføring
- [`export-person`](export-person.md) **`vindu`**
- [`Strategier for face-suggest`](face-suggest-strategier.md)
- [`face-scan`](face-scan.md) **`vindu`**
- [`face-suggest`](face-suggest.md) **`web`**
- [`face-reset`](face-reset.md) **`cli`**

Kommandoer om ansiktsgjenkjenning du kanskje ikke trenger hvis du bruker
[`run-server`](run-server.md):

- [`face-report`](face-report.md) **`cli`**
- [`face-person-create`](face-person-create.md) **`web`**
- [`face-person-add-face`](face-person-add-face.md) **`web`**
- [`face-person-remove-face`](face-person-remove-face.md) **`web`**
- [`face-person-delete`](face-person-delete.md) **`web`**
- [`face-person-rename`](face-person-rename.md) **`web`**
- [`face-person-list`](face-person-list.md) **`web`**

## Geolokalisering

- [`geo-scan`](geo-scan.md) **`vindu`**
- [`exiftool-install`](exiftool-install.md) **`cli`**
- [`geo-stats`](geo-stats.md) **`web`**
- [`geo-areas`](geo-areas.md) **`cli`**
- [`geo-area`](geo-area.md) **`cli`**
 
## Finne ting som bør kontrolleres

- [`conflicts`](conflicts.md) **`cli`**
- [`show-conflict`](show-conflict.md) **`cli`**
- [`non-metadata`](non-metadata.md) **`cli`**
- [`errors`](errors.md) **`cli`**

## Diverse

- [`explain-date`](explain-date.md) **`cli`**
- [`inspect-metadata`](inspect-metadata.md) **`cli`**
- [`refresh-metadata`](refresh-metadata.md) **`cli`**
- [`exiftool-metadata-gaps`](exiftool-metadata-gaps.md) **`devel`**

## Tekstbasert bildesøk

- [`image-scan`](image-scan.md) **`vindu`**
- [`cleanup-image-search`](cleanup-image-search.md) **`cli`**

Se den samlede innføringen: [`openclip`](openclip.md).

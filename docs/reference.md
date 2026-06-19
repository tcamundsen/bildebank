# Liste over alle kommandoer

Kommandoer merket **cli** ("command line interface") kan bare kjøres fra
PowerShell.

Merket **web** betyr at du kan få gjort det samme i nettleseren som det
kommandoen gjør i powershell. Jeg anbefaler da å bruke nettleseren ved å kjøre
`bildebank run-server` fra PowerShell.

Merket **devel** betyr at kommandoen ikke er ment for sluttbrukere. De som ikke
er merket har jeg ikke sett over ennå.

Jeg har plassert det du trenger først og mest øverst i denne filen.

## Kom i gang

- [`create`](create.md) **`cli`**
- [`import`](import.md) **`cli`**
- [`config`](config.md) **`cli`**

## Se og kontrollere samlingen

- [`run-server`](run-server.md) **`cli`**
- [`make-thumbnails`](make-thumbnails.md) **`cli`**
- [`status`](status.md) **`cli`**
- [`list-sources`](list-sources.md) **`web`**
- [`show-source`](show-source.md) **`web`**
- [`check-source`](check-source.md) **`cli`**
- [`rescan-source`](rescan-source.md) **`cli`**
- [`date-set`](date-set.md) **`cli`**
- [`date-clear`](date-clear.md) **`cli`**

## Rydde

- [`remove`](remove.md) **`web`**
- [`undelete`](undelete.md) **`web`**
- [`unimport`](unimport.md) **`cli`**
- [`list-removed`](list-removed.md) **`web`**

## Programmet

- [`where-is`](where-is.md) **`cli`**
- [`doctor`](doctor.md) **`cli`**
- [`backup`](backup.md) **`cli`**
- [`migrate`](migrate.md) **`cli`**
- [`vacuum`](vacuum.md) **`cli`**
- [`update`](update.md) **`cli`**

## Generering av statiske HTML-filer:

- [`make-browser`](make-browser.md) **`cli`**
- [`make-face-browser`](make-face-browser.md) **`cli`** **`devel`**
- [`make-people-browser`](make-people-browser.md) **`cli`**
- [`make-person-browser`](make-person-browser.md) **`cli`**

## Ansiktsgjenkjenning

- [`insightface`](insightface.md) - innføring
- [`Strategier for face-suggest`](face-suggest-strategier.md)
- [`face-config`](face-config.md) **`cli`** - gammel kompatibilitetskommando erstattet av [`config`](config.md)
- [`face-scan`](face-scan.md) **`cli`**
- [`face-suggest`](face-suggest.md) **`web`**
- [`face-reset`](face-reset.md) **`cli`**

Kommandoer om ansiktsgjenkjenning du kanskje ikke trenger hvis du bruker
[`run-server`](run-server.md):

- [`face-status`](face-status.md) **`cli`** - gammelt navn for [`doctor`](doctor.md)
- [`face-report`](face-report.md) **`cli`**
- [`face-person-create`](face-person-create.md) **`web`**
- [`face-person-add-face`](face-person-add-face.md) **`web`**
- [`face-person-remove-face`](face-person-remove-face.md) **`web`**
- [`face-person-delete`](face-person-delete.md) **`web`**
- [`face-person-rename`](face-person-rename.md) **`web`**
- [`face-person-list`](face-person-list.md) **`web`**

## Geolokalisering

- [`geo-scan`](geo-scan.md)
- [`exiftool-install`](exiftool-install.md)
- [`geo-stats`](geo-stats.md)
- [`geo-areas`](geo-areas.md)
- [`geo-area`](geo-area.md)
 
## Finne ting som bør kontrolleres

- [`conflicts`](conflicts.md)
- [`show-conflict`](show-conflict.md)
- [`non-metadata`](non-metadata.md)
- [`errors`](errors.md)

## Diverse

- [`explain-date`](explain-date.md) **`cli`**
- [`inspect-metadata`](inspect-metadata.md) **`cli`**
- [`refresh-metadata`](refresh-metadata.md) **`cli`**
- [`exiftool-metadata-gaps`](exiftool-metadata-gaps.md) **`devel`**
- [`make-conflict-browser`](make-conflict-browser.md)
- [`report`](report.md)

## Tekstbasert bildesøk

Se den samlede innføringen: [`openclip`](openclip.md).

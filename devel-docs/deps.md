# Obligatoriske deps

Jeg prøver å få oversikt over ekstra biblioteker. Det er spesielt interessant
å vite hva som er valgfritt og hva som er obligatorisk. Jeg ser for meg at vi
skal ha et web-grensesnitt som viser hvilke valgfrie ting som er tilgjengelig
og hvilke som er aktivert.

## Pillow

Brukes til å lage thumbnails. OpenClip trenger også Pillow.

Er lagt til i dependencies i pyproject.toml og installeres dermed automatisk.

## H3

Brukes til geografisk gruppering av bilder.

Er lagt til i dependencies i pyproject.toml og installeres dermed automatisk.

# Valgfrie tillegg

Bildebank har en del valgfrie eksterne avhengigheter.

## InsightFace

Brukes til ansiktsgjenkjenning.

Installeres med `install-insightface.ps1`

## Openclip

Brukes til tekstsøk etter bilder.

Installeres med `open-clip.ps1`



# status
<!-- CLI-HELP-START -->
```text
usage: bildebank status [valg]

Vis kort status for bildesamlingen

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`status` viser en kort oppsummering av bildesamlingen.

Kommandoen teller importerte bilder og videoer, og viser hvor mange filer som
har dato hentet fra metadata, filnavn eller filens endringstidspunkt (`mtime`).
Den viser også antall kilder, kildefilforekomster, duplikatkilder, uløste feil,
navnekollisjoner og filer uten dato.

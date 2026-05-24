# doctor
<!-- CLI-HELP-START -->
```text
usage: bildebank doctor [valg]

options:
  -h, --help  show this help message and exit
```
<!-- CLI-HELP-END -->

`doctor` viser en read-only diagnose for Bildebank-installasjonen og aktiv
bildesamling. Kommandoen endrer ikke config, databaser eller bildefiler.

Kommandoen viser status for ansiktsgjenkjenning, tekstbasert bildesøk,
ExifTool og, når Bildebank finner en aktiv bildesamling, status for relevante
databaser i samlingen.

Det gamle navnet `face-status` virker fortsatt som alias.

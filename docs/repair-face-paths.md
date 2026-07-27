# repair-face-paths
<!-- CLI-HELP-START -->
```text
usage: bildebank repair-face-paths [valg]

Vis eller synkroniser utdaterte stier i alle eksisterende InsightFace-
modelldatabaser når file_id, SHA-256 og mediefilen fortsatt stemmer med
hoveddatabasen.

options:
  -h, --help  show this help message and exit
  --apply     Oppdater reparerbare target_path- og target_path_key-felt.
              Ansikter, embeddings og personkoblinger endres ikke.
```
<!-- CLI-HELP-END -->

`repair-face-paths` kontrollerer utdaterte, kopierte filstier i alle
eksisterende InsightFace-modelldatabaser. Dette kan være aktuelt hvis du har
byttet mellom for eksempel `buffalo_l` og `antelopev2`, og en eldre
modelldatabase ikke fikk med seg en intern filflytting.

Kommandoen er dry-run som standard:

```powershell
bildebank repair-face-paths
```

Dry-run viser hvilke `scanned_files.target_path`,
`scanned_files.target_path_key` og `faces.target_path_key` som kan
synkroniseres med hoveddatabasen. Ingen database endres.

Før en rad regnes som reparerbar, krever Bildebank blant annet at:

- hoveddatabasen og alle berørte InsightFace-databaser er hele og har
  gjeldende schema
- det ikke finnes uavklarte filflyttinger
- filen er aktiv og har samme `file_id` og SHA-256 i hoveddatabasen og
  `scanned_files`
- filen på disk har nøyaktig databaseført størrelse og SHA-256
- databasemodell, ansiktstelling og interne face-referanser ellers er
  konsistente

Ta et nytt snapshot når dry-run viser forventede endringer. Utfør deretter
reparasjonen:

```powershell
bildebank repair-face-paths --apply
```

Apply endrer bare de tre kopierte stifeltene. Ansiktsbokser, embeddings,
personer, bekreftede koblinger, forslag, hoveddatabasen og bildefilene
beholdes uendret.

Hvis kommandoen finner SHA-256-avvik, feil modell, manglende referanser eller
andre avvik enn rene filstier, gjør den ingen reparasjon. Undersøk da
databasene, bildefilene og snapshotet før du går videre.

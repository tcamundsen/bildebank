# unimport

<!-- CLI-HELP-START -->
```text
usage: bildebank unimport [valg] --name navn

Reverser en tidligere import. Kontrollerer først at alle registrerte
kildefiler fortsatt finnes med samme innhold. Krever nøyaktig bekreftelse før
noe endres.

options:
  -h, --help   show this help message and exit
  --name NAME  Navn på importen som skal reverseres
  --dry-run    Vis hva som ville blitt gjort uten å slette filer eller endre
               databasen
```
<!-- CLI-HELP-END -->

`unimport` angrer en tidligere import.

Bruk `--dry-run` først, for å se hva som vil endres:

```powershell
bildebank unimport --dry-run --name "Sommer2023"
```

Da kontrollerer Bildebank filene og viser hva som ville blitt gjort, men endrer
ikke databasen og sletter ingen filer.

Når du kjører uten `--dry-run`, viser Bildebank en oppsummering før noe endres:

```text
Kilde: Sommer2023
Registrerte kildefiler kontrollert: 179
Filer som fjernes fra aktiv samling: 142
Filer som blir liggende fordi de også finnes i andre kilder: 37
Skriv "ja, det vil jeg" for å gjennomføre unimport:
```

For å gjennomføre må du skrive nøyaktig:

```text
ja, det vil jeg
```

Hvis du skriver noe annet, avbryter Bildebank uten å endre noe.


## Hva kommandoen gjør

`unimport` fjerner koblingen mellom bildesamlingen og én bestemt import.

Hvis en fil bare kom fra denne ene importen, fjernes filen fra den aktive
bildesamlingen.

Hvis den samme filen også finnes i en annen import, blir filen liggende. Da fjernes
bare henvisningen til importen du angrer.

Filer som mister den siste importkoblingen legges først i Bildebanks
`pending_file_deletes`-kø i samme databasetransaksjon som importkoblingene
fjernes. Databasen lagres før Bildebank prøver fysisk sletting.

Hvis Windows eller et annet program midlertidig låser en fil, er unimporten
fortsatt registrert. Filen blir stående i køen med feilmelding og kan prøves
igjen senere:

```powershell
bildebank cleanup-pending-deletes --apply
```

`--dry-run` viser hvilke filer som ville blitt lagt i køen, og hvilke som
beholdes fordi andre importer fortsatt refererer til dem.

## Dette er en destruktiv kommando

`unimport` kan fjerne filer fra den aktive bildesamlingen. Derfor er kommandoen
forsiktig.

Før den endrer noe, kontrollerer Bildebank at alle registrerte kildefiler
fortsatt finnes i kilden, og at de har nøyaktig samme innhold som da de ble
importert.

Hvis en kildefil mangler eller er endret, stopper kommandoen uten å gjøre
endringer.

## Hvis kilden mangler

`unimport` må kontrollere originalfilene før noe fjernes. Hvis kilden ligger på
USB, CD eller minnekort, må riktig medium være satt inn når du kjører
kommandoen.

Hvis Bildebank sier at en kildefil mangler, sjekk at riktig USB-disk, CD eller
minnekort er koblet til, og at den har samme stasjonsbokstav og path som da
importen ble kjørt.

Dette er gjort omstendig med hensikt, for å unngå å miste bilder. Det er
foreløpig ikke mulig å kjøre `unimport` hvis orginalfilene mangler. Hvis
noen har sterkt behov for det, kan det vurderes å legge til
`--ja-jeg-vil-miste-filer` eller lignende for å kjøre `unimport` uten
orginalfilene.

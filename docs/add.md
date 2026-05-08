# add

`add` er gammel arbeidsflyt og er ikke anbefalt for vanlig bruk.

## Bruk import i stedet

Tidligere kunne man gjøre dette:

```powershell
bildebank add "$HOME\Pictures\TestBilder"
bildebank import
```

Nå bør du bruke én kommando:

```powershell
bildebank import --name "TestBilder" "$HOME\Pictures\TestBilder"
```

Det samme gjelder USB-brikker, CD-er, minnekort og eksterne disker:

```powershell
bildebank import --name "Familie-CD-2004" E:\
```

## Hvorfor er add ikke anbefalt?

`add` registrerer en kilde uten å importere den med en gang. Det var nyttig i en
tidligere versjon av designet, men vanlig bruk er enklere når hver import skjer
direkte og får et navn.

Hovedregelen er nå:

```powershell
bildebank import --name "Navn" "path\til\kilde"
```

Hvis du senere vil angre importen:

```powershell
bildebank unimport --name "Navn"
```


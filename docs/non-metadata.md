# non-metadata

`non-metadata` lister filer der datoen ikke kom fra metadata.

## Referanse

```powershell
bildebank non-metadata
bildebank non-metadata --with-source
```

## Hva kommandoen gjør

Bildebank prøver helst å bruke dato fra metadata. Hvis det ikke finnes, kan den
bruke dato fra filnavn, filens endringstidspunkt eller ukjent dato.

`--with-source` viser også kildefilen.


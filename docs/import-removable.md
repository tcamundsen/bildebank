# import-removable

`import-removable` er erstattet av `import`.

## Bruk dette i stedet

Tidligere:

```powershell
bildebank import-removable --name "Familie-CD-2004" E:\
```

Nå:

```powershell
bildebank import --name "Familie-CD-2004" E:\
```

Den nye `import`-kommandoen brukes både for vanlige mapper, CD-er,
USB-brikker, minnekort og eksterne disker.

## Hvorfor ble kommandoen fjernet?

Det er enklere for brukeren at det finnes én måte å importere en kilde på:

```powershell
bildebank import --name "Navn" "path\til\kilde"
```

Navnet etter `--name` er identiteten til importen. Det gjelder uansett om
kilden ligger på PC-en eller på et flyttbart medium.

Hvis du senere vil angre importen, bruker du samme navn:

```powershell
bildebank unimport --name "Familie-CD-2004"
```

## Eksempel med flere mapper fra samme USB-brikke

La oss si vi har en brikke vi kaller "Brikke-A" med 3 mapper:

```powershell
F:\
 +-mappe1
 +-mappe2
 +-mappe3
```

Du kan importere enkeltmapper først, og eventuelt hele brikken senere. Bruk et
unikt navn for hver import:

```powershell
C:\fotobank> bildebank import --name "BrikkeA-1" F:\mappe1
C:\fotobank> bildebank import --name "BrikkeA-2" F:\mappe2
C:\fotobank> bildebank import --name "BrikkeA-hele" F:\
```

Etter de tre kommandoene har du importert alt på USB-brikken, uten at det
lagres duplikate bildefiler.

Hvis du vil rydde bort de to første kildehenvisningene, kan du kjøre:

```powershell
C:\fotobank> bildebank unimport --name "BrikkeA-1"
C:\fotobank> bildebank unimport --name "BrikkeA-2"
```

Test gjerne først:

```powershell
C:\fotobank> bildebank unimport --dry-run --name "BrikkeA-1"
```


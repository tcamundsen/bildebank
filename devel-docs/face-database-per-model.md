# Face-database per modell

Mål: gjøre det trygt å scanne samme bildesamling med flere
InsightFace-modeller uten å blande embeddings fra ulike modeller.

## Config

Legg til et eget felt for face-databaser:

```toml
[face_recognition]
model_name = "buffalo_l"
database_dir = ".bildebank-faces"
```

`database_dir` er relativ til aktiv bildesamling/target hvis den ikke er
absolutt. Face-databasene inneholder resultater for én bestemt bildesamling og
skal derfor ligge sammen med bildesamlingen, ikke i programrepoet.

## Fil per modell

Aktiv face-database velges fra `model_name`:

```text
bildesamling/
  .bilder.sqlite3
  .bildebank-faces/
    buffalo_l.sqlite3
    antelopev2.sqlite3
```

Filnavnet skal lages kontrollert fra modellnavnet, ikke direkte fra vilkårlig
brukertekst. Tillat enkle tegn som bokstaver, tall, punktum, bindestrek og
understrek. Avvis eller normaliser andre tegn.

## Sanity check

Hver face-database skal lagre modellen den tilhører i `meta`, for eksempel:

```text
meta.model_name = buffalo_l
```

Når databasen åpnes:

- hvis `meta.model_name` mangler i en ny database, sett den til aktiv
  `model_name`
- hvis `meta.model_name` finnes og er ulik aktiv `model_name`, stopp med en
  tydelig feil
- ikke forsøk å migrere eller blande embeddings mellom modeller

Dette gjør modellnavnet i filstien praktisk, mens `meta.model_name` beskytter
mot feilkonfigurasjon, manuell filflytting og gamle testfiler.

## Modellmappe

Modellene lagres i program-mappen, i subfolder ".bildebank-insightface".
Noen InsightFace-pakker kan ende opp med et ekstra mappenivå etter nedlasting:

```text
.bildebank-insightface/models/antelopev2/antelopev2/*.onnx
```

Ved modellinnlasting bør Bildebank normalisere dette til:

```text
.bildebank-insightface/models/antelopev2/*.onnx
```

Dette bør skje i face-modellinnlastingen, ikke i databasekoden. Reglene skal
være konservative:

- reparer bare under `model_root/models/<model_name>`
- flytt bare hvis `<model_name>/<model_name>/*.onnx` finnes og
  `<model_name>/*.onnx` mangler
- ikke overskriv eksisterende filer
- hvis layouten er uklar, stopp med en tydelig feil i stedet for å gjette
- hvis `FaceAnalysis` feiler fordi detection-modellen mangler, kan Bildebank
  prøve normalisering én gang og deretter laste modellen på nytt

## Konsekvens

`face-scan`, `face-suggest`, personer og bekreftede ansikter bruker bare aktiv
modell sin database. Sluttbruker ser fortsatt én aktiv modell, men utvikling kan
sammenligne flere modeller ved å bytte `model_name` og kjøre scan på nytt.

Forslag fra `face-suggest` skal også lagres i den modellspesifikke databasen.
Når man bytter mellom modeller, skal tidligere scannede ansikter, personer,
bekreftede personkoblinger og forslag for hver modell bevares. Det betyr at
`buffalo_l.sqlite3` og `antelopev2.sqlite3` kan ha hver sine personer,
bekreftelser og forslag, slik at modellene kan sammenlignes uten at data
overskrives.

## Kompatibilitet

Vi tar bare hensyn til én midlertidig kompatibilitetsregel fordi det trolig
bare er to brukere av ansiktsgjenkjenning.

Hvis `bildesamling/.bilder-faces.sqlite3` finnes på gammelt sted og
`bildesamling/.bildebank-faces/buffalo_l.sqlite3` ikke finnes, skal Bildebank
opprette katalogen `bildesamling/.bildebank-faces/` og flytte den gamle
databasen dit. Den gamle databasen regnes som laget med modellen `buffalo_l`.

Koden som gjør dette skal merkes med en tydelig kommentar:

```python
# KOMPATIBILITET: ...
```

Denne midlertidige kompatibilitetskoden kan fjernes når de eksisterende
brukerne har migrert.

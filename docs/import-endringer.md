## Import-endringer

Den gamle importflyten er fjernet. Det som tidligere het
`bildebank import-removable`, heter nå `bildebank import`.

Det betyr at du bruker "bildebank import" på alt, og samtidig må
gi alle enheter og mapper et navn med `--name`.

`bildebank add`, gammel `bildebank import` uten kilde, `bildebank
import-removable` og `bildebank remove-source` er fjernet.

Hva oppnår vi:

 - Enklere kode
 - Færre måter å gjøre ting på
 - Enklere å skrive tydelig dokumentasjon

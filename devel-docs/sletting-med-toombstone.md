# Permanent sletting av bilder og bruk av tombstone (slettingsmarkør)

Dette dokumentet beskriver design og konsekvenser ved å innføre permanent sletting (tømming av `deleted/`), samt hvordan konseptet **tombstone** (slettingsmarkør) forhindrer utilsiktet re-import.

---

## 1. Bakgrunn og formål

I utgangspunktet følger Bildebank prinsippet om at bilder aldri slettes permanent (med unntak av `unimport`). Ved sletting med `bildebank remove` flyttes filene til mappen `deleted/` i samlingsroten og markeres som slettet i databasen.

Brukere kan likevel ha et reelt behov for permanent sletting:
* **Privatliv og uønskede bilder:** Filer som uforvarende fulgte med fra et minnekort eller en gammel disk (f.eks. personopplysninger, feiltatte bilder eller private filer).
* **Frigjøring av diskplass:** Feiltatte 4K-videoer eller tusenvis av uskarpe serriebilder tar opp mye plass selv om de ligger under `deleted/`.

---

## 2. Problemstilling: Re-import-fellen

Dersom en bildefil slettes fra disken *og samtidig fjernes 100 % fra databasen*, oppstår en fallgruve:

1. Et bilde fra et minnekort importeres til samlingen.
2. Brukeren sletter bildet (`bildebank remove`) og tømmer papirkurven.
3. Filen slettes fra disk og fjernes helt fra databasen.
4. Senere setter brukeren inn det samme minnekortet på nytt for å hente andre bilder.
5. Bildebank skanner minnekortet, sjekker SHA-256-hashen til bildet mot databasen, finner intet spor, og **importerer bildet på nytt**.

---

## 3. Løsning: Tombstone (Slettingsmarkør)

En **tombstone** er en registrering i databasen som blir stående igjen etter at den fysiske bildefilen er slettet fra disken.

### Hvordan det fungerer
* **Fil på disk:** Slettes fra `deleted/` for å frigjøre diskplass (0 bytes opptatt).
* **Databaseoppføring:** Bevares i databasen med informasjon om at filen er eksplisitt slettet permanent av brukeren.

### Hva en tombstone inneholder
En tombstone krever minimalt med plass i databasen og lagrer typisk:
* `sha256`: SHA-256-hash av den slettede filen (brukes til duplikatsjekk).
* `size_bytes`: Opprinnelig filstørrelse (for historikk/rapportering).
* `deleted_at` / `purged_at`: Tidspunkt for når bildet ble permanent slettet.
* `status` / flagg: Indikerer at filen er permanent slettet (`purged`).

Når Bildebank senere skanner kilder med samme bilde, oppdager import-motoren at SHA-256-hashen matcher en tombstone. Importen hopper da over filen i steden for å re-importere den.

---

## 4. Konsekvenser for andre operasjoner

### 4.1. Import (`bildebank import`)
* Skanner kildemapper og beregner SHA-256.
* Hvis SHA-256 treffer en active fil $\rightarrow$ registreres som duplikatfunn.
* Hvis SHA-256 treffer en tombstone $\rightarrow$ registreres som eksplisitt forkastet/slettet, og ignoreres.

### 4.2. Angre import (`bildebank unimport`)
* Dersom brukeren kjører `unimport` på en kilde hvor enkelte bilder i ettertid har blitt permanent slettet (tombstoned):
  * `unimport` rydder opp kildekoblingen i `file_sources`.
  * Dersom ingen andre kilder refererer til samme SHA-256, kan også tombstone-oppføringen fjernes fra databasen.
  * `unimport` tolererer at den fysiske filen under `deleted/` allerede var borte.

### 4.3. Snapshots
* Historiske snapshots opprettet *før* permanent sletting vil fortsatt inneholde filen i sitt eget snapshot-arkiv på ekstern lagring.
* Nye snapshots opprettet *etter* permanent sletting vil registrere at filen i `deleted/` er borte.

---

## 5. Retningslinjer for sikkerhet og CLI-design

Dersom permanent sletting / tømming av papirkurv implementeres, må følgende sikkerhetsprinsipper overholdes:

1. **Kun via CLI:** Operasjonen skal ikke tilbys som en enkel ett-klikks-knapp i Web-UI, for å hindre uhell.
2. **Krav om `--dry-run` og bekreftelse:**
   * Kommandoen bør vise hva som vil bli slettet (antall filer og megabytes) uten å utføre endringer som standard.
   * Krever et eksplisitt bekreftelsesflagg (f.eks. `bildebank purge-deleted --confirm`).
3. **Valgfri fullstendig sletting:**
   * Dersom brukeren har et spesielt ønske om at Bildebank skal glemme filen 100 % (f.eks. ved private opplysninger der hashen heller ikke skal lagres), kan et eksplisitt flagg som `--forget-completely` vurderes, med advarsel om at re-import da kan skje ved fremtidig skanning.
4. **Loggingskrav:** Alle permanent slettede filer skal føres i `command_log`.

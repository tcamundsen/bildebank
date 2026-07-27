# Plan: Registrering av bildevisninger og oppdaging av usette bilder

## 1. Formål
Gi brukeren mulighet til å oppdage glemte og lite viste bilder i bildesamlingen ved at systemet holder oversikt over:
- Antall ganger et bilde har blitt vist (`view_count`).
- Tidspunktet bildet sist ble vist (`last_viewed_at`).

Med denne informasjonen kan brukeren be om å få vist et tilfeldig usett bilde (eller et bilde fra utvalget med færrest visninger), samt sortere bilder basert på visningsstatistikk.

---

## 2. Kjerneanvisninger og sikkerhetsregler

1. **Definisjon av en visning**:
   - Telles **kun** når et bilde vises i detaljvisning (`/display/<id>`) eller i et aktivt slideshow.
   - Miniatyrbilder i rutenett/galleri teller **ikke**.
   - For å unngå feiltelling fra prefetching eller kjapp blading med piltastene, trigges registreringen i frontend etter at bildet har vært synlig i minst **1,5 til 2 sekunder**.

2. **Feiltoleranse og read-only-håndtering**:
   - Skriving til databasen må skje asynkront og i bakgrunnen.
   - Hvis databasen kjører i `--read-only`-modus eller er midlertidig låst via `TargetLock`, skal visningsregistreringen tyst ignoreres uten å gi feilmelding eller forsinke visningen av bildet.

---

## 3. Databaseendringer (Schema v19)

### 3.1 Nødvendige felter i `files`-tabellen
- `view_count INTEGER NOT NULL DEFAULT 0`
- `last_viewed_at TEXT NULL` (ISO 8601 UTC-tidsstempel, f.eks. `2026-07-27T21:10:00Z`).

### 3.2 Indeks for rask utforsking
```sql
CREATE INDEX IF NOT EXISTS idx_files_view_stats 
ON files (deleted_at, is_image, view_count, last_viewed_at);
```

### 3.3 Migrering
- Opprette migreringsdokumentasjon: `devel-docs/database-v19-migration.md`.
- Implementere automatisk schema-migrering fra v18 til v19 i `bildebank/db_schema.py`.

---

## 4. Backend (Python / Server)

1. **Registrere visning (`POST /api/image/viewed`)**:
   - Mottar filens ID eller relativer sti.
   - Kjører `UPDATE files SET view_count = view_count + 1, last_viewed_at = ? WHERE id = ?`.
   - Ignorerer skriveoperasjonen trygt dersom serveren er i read-only-modus eller DB er opptatt.

2. **Hente tilfeldig usett/lite vist bilde (`GET /random-unseen` eller `/api/random-unseen`)**:
   - Kjører følgende utplukk:
     1. Velg et tilfeldig bilde der `view_count = 0` (om noen finnes).
     2. Dersom alle bilder har `view_count > 0`, velg et tilfeldig bilde blant de laveste 5% i `view_count`, sortert primært på eldste `last_viewed_at`.
   - Omdirigerer direkte til bildevisningen `/display/<id>`.

3. **Søkesortering**:
   - Utvide søkemotoren i Bildebank til å støtte sorteringsvalg:
     - `view_count_asc`: Færrest visninger først.
     - `last_viewed_asc`: Sist vist (eldst/aldri vist først).

---

## 5. Frontend & Brukergrensesnitt

1. **Visningstimer i nettleser (`server.js`)**:
   - Ved åpning av et bilde i detaljvisning startes en timer på 1,5 sekunder.
   - Dersom brukeren blir stående på bildet, sendes et `POST /api/image/viewed`-kall i bakgrunnen med CSRF-token.

2. **Knapper og menyvalg**:
   - Legge til knappen **«Oppdag usett bilde»** på Dashbord, i toppmenyen og i Slideshow-kontrollene.

3. **Informasjonsvisning på bildedetaljsiden**:
   - Vise en liten tekst under bildeinformasjonen:  
     `👁️ Vist 3 ganger (sist 14. feb 2025)` eller `👁️ Aldri vist tidligere`.

---

## 6. Testdekning
- **Migreringstest**: Sjekke at databaser med v18 oppgraderes feilfritt til v19.
- **Servertest**: Sjekke at `POST /api/image/viewed` øker telleren i skrivemodus, men returnerer OK/ignoreres i read-only-modus uden feil.
- **Utplukkstest**: Verifisere at `random-unseen` prioriterer bilder med `view_count = 0`.

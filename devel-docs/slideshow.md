# Slideshow på LAN

Slideshow er en begrenset modus av `run-server`, implementert i hovedsak i
`server_slideshow.py` og koblet inn via CLI-, runtime- og request-handleren.
Brukerdokumentasjon og kommandoeksempler ligger i `docs/run-server.md`.

## Varige kontrakter

- `--slideshow` innebærer LAN-binding, read-only og preview-bilder. Det er ingen
  innlogging, så LAN-advarselen skal alltid vises.
- `--delay` er et positivt antall sekunder, med standardverdi 10. `--filter`
  bruker samme parser og semantikk som Filtersøk. Ugyldig filter og tomt utvalg
  skal avvises før serverstart.
- Utvalget består av unike, aktive `files.id` for stillbilder som kan vises som
  preview. `files` er kanonisk; `file_sources` er proveniens og kan ha flere
  rader for samme fil. Kildefilter må derfor ikke lage duplikate lysbilder.
- Utvalget materialiseres i Filtersøk-rekkefølge ved oppstart. Databaseendringer
  blir først synlige etter omstart.
- Slideshowet endrer aldri database, metadata eller bildefiler.

## Sikkerhetsgrense

I slideshowmodus er bare disse rutene tilgjengelige:

```text
GET /                         minimal slideshow-side
GET /slideshow/media/<id>     generert preview for et bilde i utvalget
```

Alle POST-ruter og øvrige GET-ruter avvises. Medieruten må kontrollere at
`files.id` finnes i det materialiserte utvalget; det er ikke nok at filen finnes
i databasen. Originalfiler, slettede filer og klientoppgitte filstier skal aldri
gjøres tilgjengelige.

## Klientatferd

Siden viser ett helt bilde mot svart bakgrunn med `object-fit: contain`, laster
neste bilde på forhånd og starter delay først etter vellykket lasting. Feil på
ett bilde hopper videre. Listen går i løkke, og hver klient har egen posisjon og
klokke.

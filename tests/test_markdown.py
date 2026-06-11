from __future__ import annotations

import unittest

from bildebank.server import resolve_doc_path
from bildebank.server_pages import markdown_doc_page_html
from bildebank.server_markdown import markdown_to_html


class MarkdownTests(unittest.TestCase):
    def test_geo_help_markdown_is_rendered_as_html(self) -> None:
        doc_path = resolve_doc_path("web/steder")
        self.assertIsNotNone(doc_path)
        assert doc_path is not None

        html = markdown_doc_page_html(doc_path, doc_path.read_text(encoding="utf-8"))

        self.assertIn("<h1>Bruk av GPS-lokasjon i bilder</h1>", html)
        self.assertIn("<code>/geo</code>", html)
        self.assertIn("<strong>Egendefinerte steder</strong>", html)
        self.assertIn("statistikk over bilder med og uten GPS-lokasjon", html)
        self.assertNotIn("# Steder", html)

    def test_markdown_help_renderer_omits_cli_help_markers(self) -> None:
        html = markdown_to_html(
            """# Import

<!-- CLI-HELP-START -->
usage: bildebank import [valg]
<!-- CLI-HELP-END -->

Vanlig dokumentasjon.
"""
        )

        self.assertIn("<h1>Import</h1>", html)
        self.assertIn("Vanlig dokumentasjon.", html)
        self.assertIn("usage: bildebank import", html)
        self.assertNotIn("CLI-HELP-START", html)
        self.assertNotIn("CLI-HELP-END", html)

    def test_markdown_help_renderer_supports_numbered_lists(self) -> None:
        html = markdown_to_html(
            """Først:

1. Les `README`
2. Kjør **import**
3. Se [hjelpen](help.md)

- Ferdig
"""
        )

        self.assertIn(
            '<ol><li>Les <code>README</code></li><li>Kjør <strong>import</strong></li><li>Se <a href="help.md">hjelpen</a></li></ol>',
            html,
        )
        self.assertIn("<ul><li>Ferdig</li></ul>", html)

    def test_markdown_help_renderer_supports_wrapped_list_items(self) -> None:
        html = markdown_to_html(
            """- Første punkt går
  over flere linjer
- Andre punkt

1. Nummerert punkt går
   også over flere linjer
2. Siste punkt
"""
        )

        self.assertIn(
            "<ul><li>Første punkt går over flere linjer</li><li>Andre punkt</li></ul>",
            html,
        )
        self.assertIn(
            "<ol><li>Nummerert punkt går også over flere linjer</li><li>Siste punkt</li></ol>",
            html,
        )

    def test_markdown_help_renderer_supports_tables(self) -> None:
        html = markdown_to_html(
            """| Tast | Hva skjer |
| --- | --- |
| Pil venstre | Forrige bilde eller video |
| Pil høyre | Neste bilde eller video |
| Pil opp | Forrige måned |
| Pil ned | Neste måned |
| Page Up | Forrige år |
| Page Down | Neste år |
"""
        )

        self.assertIn("<table><thead><tr><th>Tast</th><th>Hva skjer</th></tr></thead><tbody>", html)
        self.assertIn("<tr><td>Pil venstre</td><td>Forrige bilde eller video</td></tr>", html)
        self.assertIn("<tr><td>Page Down</td><td>Neste år</td></tr>", html)

    def test_markdown_help_renderer_supports_warning_alerts(self) -> None:
        html = markdown_to_html(
            """> [!WARNING]
> `backup` lager en speiling av bildesamlingen.
> Når backup oppdateres, kan filer også slettes fra backupen.
> Ha derfor flere backup-disker som oppdateres på ulike tidspunkt.

Neste avsnitt.
"""
        )

        self.assertIn('<div class="markdown-alert markdown-alert-warning">', html)
        self.assertIn('class="markdown-alert-title"', html)
        self.assertIn("Warning", html)
        self.assertIn(
            "<p><code>backup</code> lager en speiling av bildesamlingen.<br>"
            "Når backup oppdateres, kan filer også slettes fra backupen.<br>"
            "Ha derfor flere backup-disker som oppdateres på ulike tidspunkt.</p>",
            html,
        )
        self.assertIn("<p>Neste avsnitt.</p>", html)


if __name__ == "__main__":
    unittest.main()

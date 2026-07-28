from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.join_docs import find_markdown_files, join_markdown_files


class JoinDocsTests(unittest.TestCase):
    def test_find_markdown_files_finds_and_sorts_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            docs_dir = tmp_dir / "docs"
            web_dir = docs_dir / "web"
            web_dir.mkdir(parents=True)

            (docs_dir / "b.md").write_text("Dokument B", encoding="utf-8")
            (docs_dir / "a.md").write_text("Dokument A", encoding="utf-8")
            (web_dir / "c.md").write_text("Dokument C", encoding="utf-8")
            (docs_dir / "output.md").write_text("Gammel utdata", encoding="utf-8")

            files = find_markdown_files(docs_dir, exclude_file=docs_dir / "output.md")
            file_names = [f.name for f in files]

            self.assertEqual(file_names, ["a.md", "b.md", "c.md"])

    def test_join_markdown_files_includes_markers_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            docs_dir = tmp_dir / "docs"
            web_dir = docs_dir / "web"
            web_dir.mkdir(parents=True)

            doc1 = docs_dir / "start.md"
            doc2 = web_dir / "gui.md"

            doc1.write_text("# Start\n<!-- CLI-HELP-START -->\nusage: foo\n<!-- CLI-HELP-END -->\nVelkommen.", encoding="utf-8")
            doc2.write_text("# GUI\n\nNettleser-grensesnitt.", encoding="utf-8")

            res_with_markers = join_markdown_files([doc1, doc2], base_dir=docs_dir, include_markers=True)
            self.assertIn("## Fil: docs/start.md", res_with_markers)
            self.assertIn("# Start\nusage: foo\nVelkommen.", res_with_markers)
            self.assertNotIn("<!-- CLI-HELP-START -->", res_with_markers)
            self.assertNotIn("<!-- CLI-HELP-END -->", res_with_markers)
            self.assertIn("## Fil: docs/web/gui.md", res_with_markers)
            self.assertIn("# GUI\n\nNettleser-grensesnitt.", res_with_markers)

            res_no_markers = join_markdown_files([doc1, doc2], base_dir=docs_dir, include_markers=False)
            self.assertNotIn("## Fil:", res_no_markers)
            self.assertIn("# Start\nusage: foo\nVelkommen.\n\n# GUI\n\nNettleser-grensesnitt.", res_no_markers)


if __name__ == "__main__":
    unittest.main()

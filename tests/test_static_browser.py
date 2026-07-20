from __future__ import annotations

import unittest
from pathlib import Path

from bildebank.html_export import render_html
from bildebank.static_browser import static_browser_item


class StaticBrowserItemTests(unittest.TestCase):
    def test_static_browser_item_uses_same_date_and_rotation_for_source_and_export_paths(self) -> None:
        item = {
            "id": 7,
            "target_path": "2026/01/IMG.jpg",
            "stored_filename": "IMG.jpg",
            "taken_date": "2026-01-02",
            "date_source": "metadata",
            "manual_date_from": "2004-06-01",
            "manual_date_to": "2004-08-31",
            "manual_date_note": "Kamera hadde feil dato",
            "view_rotation_degrees": 90,
            "comment": "Første linje\nAndre linje",
            "size_bytes": 123,
        }

        source_item = static_browser_item(item, Path("2026/01/IMG.jpg"), thumbnail_src="")
        export_item = static_browser_item(item, Path("2004/07/IMG.jpg"), thumbnail_src="", browser_date="2004-07-16")

        self.assertEqual(source_item["browserDate"], "2004-07-16")
        self.assertEqual(export_item["browserDate"], "2004-07-16")
        self.assertEqual(source_item["monthKey"], "2004-07")
        self.assertEqual(export_item["monthKey"], "2004-07")
        self.assertEqual(source_item["viewRotation"], 90)
        self.assertEqual(export_item["viewRotation"], 90)
        self.assertEqual(source_item["dateText"], "ca. 2004-07-16 (manuell dato)")
        self.assertEqual(export_item["manualDateNote"], "Kamera hadde feil dato")
        self.assertEqual(source_item["comment"], "Første linje\nAndre linje")

    def test_static_browser_uses_text_content_and_script_safe_embedded_comment(self) -> None:
        item = static_browser_item(
            {
                "id": 1,
                "stored_filename": "a.jpg",
                "size_bytes": 1,
                "comment": "hei\n</script><script>alert(1)</script>",
            },
            Path("2024/01/a.jpg"),
        )

        body = render_html([item])

        self.assertIn('"comment": "hei\\n\\u003c/script\\u003e', body)
        self.assertIn("overlay.textContent = item.comment", body)
        self.assertIn("white-space: pre-wrap", body)
        self.assertNotIn("</script><script>alert(1)</script>", body)

    def test_static_browser_item_sets_kind_and_thumbnail_by_media_type(self) -> None:
        image_item = {"id": 1, "stored_filename": "a.jpg", "size_bytes": 1}
        video_item = {"id": 2, "stored_filename": "a.mp4", "size_bytes": 1}

        image = static_browser_item(image_item, Path("2024/01/a.jpg"))
        video = static_browser_item(video_item, Path("2024/01/a.mp4"))

        self.assertEqual(image["kind"], "image")
        self.assertEqual(image["thumbnailSrc"], "2024/01/a.jpg")
        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["thumbnailSrc"], "")

    def test_static_browser_item_supports_minimal_person_browser_items(self) -> None:
        item = {
            "fileId": 42,
            "path": "udatert/person.jpg",
            "url": "udatert/person.jpg",
            "thumbnailSrc": "thumbs/person.jpg",
            "viewRotation": 180,
            "name": "person.jpg",
            "monthKey": "udatert",
            "sizeText": "12 byte",
        }

        browser_item = static_browser_item(
            item,
            Path(str(item["path"])),
            url=str(item["url"]),
            thumbnail_src=str(item["thumbnailSrc"]),
            kind="image",
            view_rotation=item["viewRotation"],
            name=str(item["name"]),
            month_key=str(item["monthKey"]),
        )

        self.assertEqual(browser_item["fileId"], 42)
        self.assertEqual(browser_item["browserDate"], "9999-99-99")
        self.assertEqual(browser_item["monthKey"], "udatert")
        self.assertEqual(browser_item["dateText"], "")
        self.assertEqual(browser_item["manualDateFrom"], "")
        self.assertEqual(browser_item["sizeText"], "12 byte")


if __name__ == "__main__":
    unittest.main()

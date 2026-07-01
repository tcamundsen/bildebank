from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path

from bildebank.browser_dates import (
    UNKNOWN_BROWSER_DATE,
    browser_date_from_item,
    browser_date_text,
    manual_date_midpoint,
    month_key_for_item,
    month_key_from_browser_date_value,
    month_key_from_path,
    month_key_from_stored_path,
    next_month_key,
    valid_day_key,
    valid_month_key,
)


class BrowserDatesTests(unittest.TestCase):
    def test_manual_date_midpoint_uses_center_of_range(self) -> None:
        self.assertEqual(
            manual_date_midpoint("2004-06-01", "2004-08-31"),
            dt.date(2004, 7, 16),
        )

    def test_manual_date_wins_over_taken_date(self) -> None:
        item = {
            "taken_date": "2026-01-02",
            "manual_date_from": "2004-07-15",
            "manual_date_to": "2004-07-15",
        }

        self.assertEqual(browser_date_from_item(item), "2004-07-15")
        self.assertEqual(month_key_for_item(Path("/bilder"), {**item, "target_path": "2026/01/a.jpg"}), "2004-07")

    def test_between_manual_date_uses_midpoint_for_browser_date_and_text(self) -> None:
        item = {
            "taken_date": "2026-01-02",
            "date_source": "metadata",
            "manual_date_from": "2004-06-01",
            "manual_date_to": "2004-08-31",
        }

        self.assertEqual(browser_date_from_item(item), "2004-07-16")
        self.assertEqual(browser_date_text(item), "ca. 2004-07-16 (manuell dato)")

    def test_missing_or_invalid_taken_date_uses_unknown_browser_date(self) -> None:
        self.assertEqual(browser_date_from_item({}), UNKNOWN_BROWSER_DATE)
        self.assertEqual(browser_date_from_item({"taken_date": "ikke en dato"}), UNKNOWN_BROWSER_DATE)

    def test_browser_date_text_uses_taken_date_and_source_when_no_manual_date(self) -> None:
        item = {"taken_date": "2024-12-24", "date_source": "filename"}

        self.assertEqual(browser_date_text(item), "2024-12-24 (filename)")

    def test_month_key_from_browser_date_value_requires_browser_date_shape(self) -> None:
        self.assertEqual(month_key_from_browser_date_value("2024-12-24"), "2024-12")
        self.assertIsNone(month_key_from_browser_date_value(UNKNOWN_BROWSER_DATE))
        self.assertIsNone(month_key_from_browser_date_value("2024-12"))

    def test_month_key_from_path_uses_year_month_udatert_or_ukjent(self) -> None:
        self.assertEqual(month_key_from_path(Path("2024/12/jul.jpg")), "2024-12")
        self.assertEqual(month_key_from_path(Path("udatert/gammelt.jpg")), "udatert")
        self.assertEqual(month_key_from_path(Path("annet/gammelt.jpg")), "ukjent")

    def test_month_key_from_stored_path_handles_slashes_and_validates_month(self) -> None:
        self.assertEqual(month_key_from_stored_path("C:\\bilder\\2024\\12\\jul.jpg"), "2024-12")
        self.assertEqual(month_key_from_stored_path("/bilder/2024/12/jul.jpg"), "2024-12")
        self.assertIsNone(month_key_from_stored_path("/bilder/2024/13/jul.jpg"))

    def test_month_key_for_item_falls_back_to_stored_path_and_relative_path(self) -> None:
        target = Path("/bilder")
        self.assertEqual(
            month_key_for_item(target, {"target_path": "/bilder/2024/12/jul.jpg"}),
            "2024-12",
        )
        self.assertEqual(
            month_key_for_item(target, {"target_path": "/bilder/udatert/gammelt.jpg"}),
            "udatert",
        )

    def test_month_and_day_key_validation(self) -> None:
        self.assertTrue(valid_month_key("2024-12"))
        self.assertFalse(valid_month_key("2024-13"))
        self.assertEqual(next_month_key("2024-12"), "2025-01")
        self.assertEqual(next_month_key("2024-02"), "2024-03")
        self.assertIsNone(next_month_key("2024-13"))
        self.assertTrue(valid_day_key("2024-02-29"))
        self.assertFalse(valid_day_key("2023-02-29"))


if __name__ == "__main__":
    unittest.main()

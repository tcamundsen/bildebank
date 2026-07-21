from __future__ import annotations

import datetime as dt
import unittest

from bildebank.manual_dates import add_months, date_range_from_uncertainty, days_in_month


class ManualDatesTests(unittest.TestCase):
    def test_date_range_supports_days_and_weeks(self) -> None:
        center = dt.date(2024, 6, 15)

        self.assertEqual(
            date_range_from_uncertainty(center, "3d"),
            (dt.date(2024, 6, 12), dt.date(2024, 6, 18)),
        )
        self.assertEqual(
            date_range_from_uncertainty(center, "2 uker"),
            (dt.date(2024, 6, 1), dt.date(2024, 6, 29)),
        )

    def test_add_months_crosses_year_boundary(self) -> None:
        self.assertEqual(add_months(dt.date(2024, 11, 15), 3), dt.date(2025, 2, 15))
        self.assertEqual(add_months(dt.date(2024, 2, 15), -3), dt.date(2023, 11, 15))

    def test_add_months_clamps_month_end_for_days_29_through_31(self) -> None:
        self.assertEqual(add_months(dt.date(2023, 1, 29), 1), dt.date(2023, 2, 28))
        self.assertEqual(add_months(dt.date(2024, 1, 30), 1), dt.date(2024, 2, 29))
        self.assertEqual(add_months(dt.date(2024, 3, 31), 1), dt.date(2024, 4, 30))

    def test_days_in_month_handles_leap_year(self) -> None:
        self.assertEqual(days_in_month(2023, 2), 28)
        self.assertEqual(days_in_month(2024, 2), 29)
        self.assertEqual(days_in_month(2024, 12), 31)

    def test_year_uncertainty_clamps_leap_day(self) -> None:
        self.assertEqual(
            date_range_from_uncertainty(dt.date(2024, 2, 29), "1y"),
            (dt.date(2023, 2, 28), dt.date(2025, 2, 28)),
        )

    def test_invalid_uncertainty_format_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"Ugyldig usikkerhet\. Bruk for eksempel 3d, 2w, 1m eller 1y\.",
        ):
            date_range_from_uncertainty(dt.date(2024, 6, 15), "about one month")

    def test_long_invalid_uncertainty_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"Ugyldig usikkerhet\. Bruk for eksempel 3d, 2w, 1m eller 1y\.",
        ):
            date_range_from_uncertainty(dt.date(2024, 6, 15), "9" * 100_000 + "!")

    def test_zero_uncertainty_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, r"Usikkerhet må være minst 1\."):
            date_range_from_uncertainty(dt.date(2024, 6, 15), "0m")

    def test_unknown_uncertainty_unit_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"Ugyldig usikkerhetsenhet\. Bruk d, w, m eller y\.",
        ):
            date_range_from_uncertainty(dt.date(2024, 6, 15), "1q")


if __name__ == "__main__":
    unittest.main()

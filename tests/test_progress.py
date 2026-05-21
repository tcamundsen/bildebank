from __future__ import annotations

import unittest
from io import StringIO

from bilder.progress import ProgressLine, ProgressMeter, format_duration


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


class ProgressLineTests(unittest.TestCase):
    def test_overwrites_when_stream_is_tty(self) -> None:
        stream = TtyStringIO()
        progress = ProgressLine(stream)

        progress.write("første")
        progress.write("andre")
        progress.finish()

        self.assertEqual(stream.getvalue(), "\rførste\033[K\randre\033[K\n")

    def test_uses_normal_lines_when_stream_is_not_tty(self) -> None:
        stream = StringIO()
        progress = ProgressLine(stream)

        progress.write("første")
        progress.write("andre")
        progress.finish()

        self.assertEqual(stream.getvalue(), "første\nandre\n")

    def test_progress_meter_writes_eta_on_same_tty_line(self) -> None:
        stream = TtyStringIO()
        progress = ProgressMeter("Scan", stream=stream)

        progress.update(1, 10, action="scannet", details="feil=0", eta=True)
        progress.done()

        output = stream.getvalue()
        self.assertIn("\rScan: scannet=1/10, feil=0, gjenstår=beregner\033[K", output)
        self.assertTrue(output.endswith("\n"))

    def test_progress_meter_error_finishes_active_tty_line_first(self) -> None:
        stream = TtyStringIO()
        progress = ProgressMeter("Scan", stream=stream)

        progress.update(1, 10, action="scannet", force=True)
        progress.error("Scan-feil: bilde.jpg")

        self.assertEqual(stream.getvalue(), "\rScan: scannet=1/10\033[K\nScan-feil: bilde.jpg\n")

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(5), "5s")
        self.assertEqual(format_duration(65), "1m 05s")
        self.assertEqual(format_duration(3660), "1t 01m")
